# tests/

Smoke test suite for the Daily Flash pipeline.

**Scope**: smoke tests, not regression tests. Catches structural breakage (missing modules, missing payload keys, stale cron) — not business logic edge cases.

**Read-only**: tests query Supabase but never mutate production data. Safe to run anytime.

---

## Running locally

```bash
cd ~/daily-flash
source .venv/bin/activate           # or however you activate your env
pip install -r requirements.txt     # ensure pytest installed
pytest                              # run all tests
pytest tests/test_imports.py        # run a single test file
pytest -v                           # verbose output
pytest --tb=short                   # shorter tracebacks
```

Tests pick up Supabase credentials from `.env` at the repo root (same source as the Python pipeline uses, via `src/supa.py`). If `.env` is missing or has wrong values, `test_payload.py` will skip with a clear error.

**Expected runtime**: ~5-10 seconds for the full suite (most time is in `test_payload.py` queries).

### ⚠️ Local `.env` may point to a stale/abandoned Supabase project

Discovered during the Tier 2 #3 implementation (2026-05-14): the local `.env` may have `SUPABASE_URL` pointing to a project that's no longer the production target. Symptoms:

- `test_payload.py` skips all tests with message *"No flash_reports rows in the last 7 days"*
- A standalone Python query (`sb.table("flash_reports").select(...).limit(5).execute()`) returns only very old rows

If you see this, your `.env`'s `SUPABASE_URL` likely points at an early-Phase 1 dedicated project (e.g., `iylnwafwrvzwkhhskazu.supabase.co`) rather than the current Lovable Cloud project (`wgbghdbfmapuqbfeiygb.supabase.co` for Daios Cove). Update the `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` to the production project to make local tests query real data.

Railway production environment variables override `.env`, so the cron service on Railway has been running against the correct project all along. This is purely a local-dev gotcha.

---

## What each file tests

### `test_imports.py` — module import smoke

Every `src/*.py` module imports without error. Catches:

- Python syntax errors
- Missing import dependencies (e.g., new import without `requirements.txt` update)
- Import-time misconfiguration
- Circular imports
- Module-level errors that would crash the cron at startup

**Should be the first thing to fail** if a deploy is broken. Doesn't touch the database.

### `test_payload.py` — latest `flash_reports` payload structure

Validates the most recent `flash_reports` row:

- `computed_at` within 48 hours (cron is alive)
- `report_date` is today / tomorrow (date logic correct)
- All Phase 60-62 grids present (`pool_heating_grid`, `pool_cleaning_grid`, `pool_fence_grid`, `cribs_grid`)
- Room count invariants: 47 heating rooms, 137 cleaning rooms, 137 fence rooms
- Each grid entry has expected shape (e.g., `pool_heating_grid` items have `room`, `type_code`, `is_heated_today`)
- Legacy `pool_heating` is empty `[]` (Phase 60 follow-up soak window)

**What this catches**: any future migration that accidentally drops a payload field, or any pipeline change that breaks an expected key. The Phase 67.1 column-name bug class would have been caught here if it had affected the payload structure.

**What this does NOT catch**: full pipeline execution (no fresh xlsx → envelope), business logic edge cases, data quality issues.

---

## Adding tests

When adding a new test:

1. **Smoke level only** — assert structural invariants, not business outcomes. If "pool heating grid has 47 rooms" changes, the test should fail (and you legitimately update it). If "Mueller pool is heated today" changes, that's not a structural concern.

2. **Read-only** — never INSERT / UPDATE / DELETE from a test. If you need to test write paths, mock at the supabase client layer.

3. **Useful failure messages** — when a test fails, the message should tell the operator what to investigate. Bad: `assert x == y`. Good: `assert ..., f"pool_heating_grid has {len(grid)} entries, expected 47. Check src/heatable_rooms.py registry."`

4. **Fixtures via conftest.py** — share Supabase client + repo path setup across files. Don't duplicate.

5. **Parametrize for similar cases** — e.g., one test per expected payload key, generated via `pytest.mark.parametrize`. Cleaner than one big test with 16 assertions.

---

## CI integration

Tests run automatically on every push to `main` via `.github/workflows/smoke.yml`.

**Required GitHub secrets** (set in `Settings → Secrets and variables → Actions`):
- `SUPABASE_URL` — same as Railway env var
- `SUPABASE_SERVICE_ROLE_KEY` — same as Railway env var

If the secrets are missing, the workflow run fails at the env-var check step with a clear message.

---

## When tests fail

| Failing test | First thing to check |
|---|---|
| `test_imports.py::test_module_imports_without_error[X]` | Open `src/X.py` and try `python -c "import X"` locally. Read the assertion error for the underlying exception. |
| `test_payload.py::test_latest_report_recent` | Cron may have stopped. Check Railway → Deployments + cron logs + the BetterUptime heartbeat. |
| `test_payload.py::test_payload_key_exists[X]` | The payload is missing key X. Check `src/daily.py::assemble_payload_in_memory()` to confirm the key is being written. |
| `test_payload.py::test_pool_heating_grid_room_count_invariant` | Either the room registry changed (legitimate — update `src/heatable_rooms.py`) or the pipeline regressed (investigate). |
| `test_pool_heating_soft_deprecated` | Either the soak period ended and the field was fully removed (update test to assert absence), or the pipeline regressed and is writing data into the deprecated field again. |

---

## Limitations / future work

- **No end-to-end pipeline test.** A real "run daily.py against a fixture xlsx and check the envelope" test would catch more, but requires a checked-in xlsx fixture + dry-run mode in `daily.py`. Deferred.
- **No edge function tests.** The Lovable Cloud edge functions (PDF generation, email send) aren't tested here. They're tested by humans through the morning briefing arriving correctly.
- **No idempotency tests.** Don't currently verify that running the same migration twice is safe — we trust the `if not exists` idempotency guards.
- **No CI for migrations.** A future Tier 3 item: lint SQL migrations + validate column references against schema before merging.
