# 8 May 2026 — Pool heating thread closure + PostgREST cache eradication

Backup of the Notion entry (Notion API was timing out at write time; retry pending).

## Closure

Direct HTTP inspection of PostgREST's OpenAPI endpoint (`/rest/v1/`) confirmed
the cache holds **19 paths / 8 RPCs** out of Postgres' **307 functions** in
public schema. The cache is frozen to whatever loaded at process startup time;
every newer object is invisible. Notify-pgrst, COMMENT DDL changes, GRANT
changes — all ignored by the running instance.

Lovable's response after we shared the diagnosis:

> Confirmed: backend DB is healthy and `explore_pool_heating` exists, so this
> is a PostgREST cache/process issue; I've sent the restart/cache-rebuild
> request to the Lovable team, and the current in-memory dashboard workaround
> remains safe to use.

State as of close of 8 May:

- Pool heating dashboard live and accurate (37 villas) via Phase 50.2 in-memory
  compute. No PostgREST dependency on the data path.
- Phase 49 SQL function correct in DB. Phase 51 grants applied. Both ready to
  be picked up the moment PostgREST refreshes.
- Lovable infra team holds the restart action. Could be hours, could be days.
  No operational pressure — the workaround is robust.

## Tomorrow morning's verification

If PostgREST restarted overnight:

```python
sb.rpc('explore_pool_heating', {
  'p_start': date.today().isoformat(),
  'p_end': (date.today() + timedelta(days=14)).isoformat(),
}).execute()
```

If it returns rows, restart happened. We can revert daily.py's pool heating
path back to the RPC for cleanliness, or leave it on the in-memory compute
(faster, more reliable). Either is correct.

If it still 404s, stay on the workaround until Lovable's team gets to it.

## Architecture takeaway

The four-role GRANT convention is now non-negotiable for any new view or RPC
migration in this codebase:

```sql
grant select on <view> to authenticator, anon, authenticated, service_role;
grant execute on function <fn>(...) to authenticator, anon, authenticated, service_role;
```

Why: PostgREST connects as `authenticator` for schema cache discovery. Missing
that grant silently breaks the object's visibility through the API layer, even
if the API roles (`anon`, `authenticated`, `service_role`) all have access.
Postgres-level tests will pass; HTTP-layer calls will fail with PGRST205.

This is the most subtle bug class we've shipped against — worth auditing the
existing migration history for similar gaps.

## Saga in commits

| SHA | Phase | Description |
|---|---|---|
| `acb2167` | 50 | populate flash_report.payload.pool_heating from explore_pool_heating RPC |
| `4583a5f` | 50.1 | fall back to view query when RPC cache fails |
| `b4381b7` | 50.2 | compute in-memory from records (no DB read) |
| `d3b591b` | 51 | GRANT to authenticator role for PostgREST cache discovery |
