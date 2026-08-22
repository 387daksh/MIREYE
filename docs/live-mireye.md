# Live MIREYE Integration

Normal tests remain offline and use a per-run temporary workspace database:

```powershell
pytest -q
```

Run the explicit live integration path only with credentials available through
`MIREYE_API_KEY` and optionally `MIREYE_BASE_URL`:

```powershell
python scripts/live_mireye_demo.py --report $env:TEMP\mireye-live-report.json
```

It uses one public address, a two-location batch with one shared field list,
and a temporary SQLite database. It never targets `app/data/workspaces.db`.
The report excludes credentials and records live contract version, quote data,
snapshot IDs, refresh results, and any discovered incompatibility.

## Verified credit model

Verified against MIREYE API/catalog version `0.15.0` on the Build plan:

- `POST /v1/fetch/quote` is unmetered. Its `credits_total` is computed by the
  same pricing functions used by fetch billing.
- `POST /v1/lookup` without parcel resolution costs the geocode price. With
  `include_parcel=true`, it costs the plan's `resolve_credits`: 300 credits on
  Build.
- A fetch containing any parcel-record-group field costs 300 credits per
  location once, regardless of how many parcel fields are selected, plus one
  credit for every selected field outside that group.
- Batch applies the same calculation independently per location. The verified
  four-field parcel batch was 300 credits per location, or 600 for two.
- Refresh is a normal fetch. Re-fetching unchanged fields is still metered;
  cache/freshness planning avoids the request but does not discount it after
  execution.
- OpenAPI, field/plan catalog, usage reads, and quote calls are unmetered.
- Failed request validation (`422`) and an invalid-field quote (`400`) did not
  change the account balance.

The bounded integration run written at `2026-08-20T13:53:31.9965368Z`
reconciled exactly:

| Order | Operation | Locations | Fields | Quote | Charge |
|---:|---|---:|---:|---:|---:|
| 1 | Lookup with parcel resolution | 1 | n/a | n/a | 300 |
| 2 | T1 `/v1/fetch` | 1 | 34 | 322 | 322 |
| 3 | `/v1/fetch/batch` | 2 | 4 shared fields | 600 | 600 |
| 4 | T2 refresh `/v1/fetch` | 1 | 8 | 302 | 302 |
| | **Total** | | | | **1524** |

The runner also made duplicate safety quotes immediately before T1 fetch and
for both refresh plans. Those quotes cost zero. The expired refresh plan never
executed a fetch, so it cost zero. Individual request timestamps and request IDs
were not captured by that historical run; subsequent runs now include a
`request_ledger` with request order, timestamps, endpoint, request ID when
provided, status, fields, location count, metering classification, and quote
credits.

For the browser demo after provisioning a snapshot, use the existing command:

```powershell
python -m app.sandbox_demo --confirm --serve
```
