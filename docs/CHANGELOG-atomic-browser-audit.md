# Merge: atomic browser audit logging (from `laples`) into `main`

This merge ports the per-action audit-logging feature developed on the
`laples` branch into `main`, without touching anything `main`-only
(the `fe/` demo UI and the `ASK_USER` clarification flow are untouched).

## What changed

1. **`app/config/settings.py`**
   - New flag `ATOMIC_BROWSER_AUDIT: bool = False`. Off by default so
     existing behavior/tests are unaffected until opted in per environment.

2. **`app/domains/agent/services/browser_prototype_agent.py`**
   - New function `run_browser_prototype_agent_atomic()`: runs a batch of
     browser steps in one Playwright session (same session-reuse as the
     existing combined path) but writes **one `audit_logs` row per step**
     (own `action_id`, shared `run_id`) instead of one combined row for the
     whole batch. Purely additive — nothing calls it unless the flag is on.
   - Small bugfix carried over from `laples`: `_write_audit()` now uses
     `get_audit_repository()` (respects `settings.AUDIT_BACKEND`) instead of
     hardcoding `AuditRepositoryDB()`.

3. **`app/domains/agent/services/agent_loop.py`**
   - New `_execute_browser_batch_atomic()` helper.
   - `_execute_browser_batch()` now gates on the flag, right after the
     existing "no URL" check and the `executing` event emit, and *after*
     `main`'s own sanitize/`ASK_USER` handling further up the call chain —
     so clarification pauses still happen before any atomic execution:
     ```python
     if get_settings().ATOMIC_BROWSER_AUDIT:
         return await _execute_browser_batch_atomic(run, steps, url, actions)
     ```
   - On a navigation-level failure (before any per-step row can be written),
     every step in the batch is written as a `FAILED` row individually so
     nothing silently disappears from the audit trail. On a mid-batch action
     failure, the remaining steps are written `SKIPPED`.

4. **Tests** (`tests/unit/test_agent_loop.py`)
   - `laples` had no dedicated atomizer tests (its differing tests were only
     due to missing `ASK_USER`/sensitive-field features, which don't apply
     here), so two new tests were added:
     - `test_atomic_browser_audit_writes_one_row_per_step`
     - `test_atomic_browser_audit_marks_remaining_steps_skipped_on_failure`
   - The one existing test that stubs `get_settings()` wholesale was updated
     to include `ATOMIC_BROWSER_AUDIT=False` so it keeps exercising the
     non-atomic path unchanged.

## What was intentionally NOT ported

- `laples`'s `.env*`, `Dockerfile`, `docker-compose.yml` — local/environment
  config, not a feature.
- `database-url-fallback.patch` — small unrelated docker-compose fix from
  `laples`; can be applied separately if wanted.
- `laples`'s `artifacts/audit/events.jsonl` and `data/browser/screenshots/`
  — local run artifacts, not source.

## Verification

- `python -m py_compile` clean on all three modified modules.
- `pytest tests/unit` — same 7 pre-existing (environment/dependency-related,
  unrelated to this change) failures as unmodified `main`; all other tests
  pass, including the 2 new atomizer tests.

## Rollout

1. Merge with `ATOMIC_BROWSER_AUDIT=False` (default) — no behavior change.
2. Enable in staging (`.env.staging`), run a few multi-step browser flows,
   confirm `audit_logs` rows are now one-per-action for those runs.
3. Enable in production once verified.
