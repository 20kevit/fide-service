# FIDE Service — API v1 Contract (FROZEN, Phase 2)

Base: `/api/v1`. JSON only (`application/json; charset=utf-8`).
Breaking changes require a new version prefix (`/api/v2`); additive
optional fields are allowed under v1. Frozen 2026-09-07.

## Authentication (frozen)

- Reads: header `X-API-Key: <FIDE_API_KEY>`.
- Admin: header `X-Admin-Key: <FIDE_ADMIN_KEY>` (or `X-API-Key` carrying
  the admin key when the keys coincide; when `FIDE_ADMIN_KEY` is unset
  the read key authorizes admin).
- Comparison: `hmac.compare_digest`. Fail closed.
- Missing/invalid credentials → `401 UNAUTHORIZED`; a valid READ key on
  an admin endpoint → `403 FORBIDDEN`; unconfigured service → `503`.
- Keys travel in headers only — never URLs, bodies, logs, or responses.
- Rate limit: per-key sliding 60 s window, default 600/min
  (`RATE_LIMIT_PER_MINUTE`; 0 disables for local dev/tests only).
  Exceeding → `429 RATE_LIMITED` with `Retry-After` seconds. State is
  process-local and bounded (stale keys purged past 5,000 tracked keys).

## Error envelope (frozen)

```json
{"error": {"code": "NOT_FOUND", "message": "Player 1 not found.",
           "details": {}, "request_id": "…"}}
```

Stable codes: `BAD_REQUEST` (400), `UNAUTHORIZED` (401),
`FORBIDDEN` (403), `NOT_FOUND` (404), `CONFLICT` (409),
`RATE_LIMITED` (429), `SERVICE_UNAVAILABLE` (503), `INTERNAL` (500).
No tracebacks, DB errors, or secrets — ever.

## Dataset block (frozen, on every data response)

```json
{"dataset": {"period": "2026-09", "imported_at": "…Z",
             "source": "fide-official-xml"}}
```

Swiss MUST NOT infer the dataset — it is always stated. Each rating
object additionally carries its own `period`.

## Endpoints (frozen)

### `GET /healthz` — open liveness probe → `{"ok": true}` (no auth).

### `GET /status` — read key. Service + dataset overview:
`{data: {ok, players, ratings, by_type, periods, latest_period,
last_import: {period, status, jobs, imported_at},
recent_jobs_by_state}, dataset}`.

### `GET /players/{fide_id}[?include=ratings|none]` — read key.
`data: {fide_id, name, federation, sex, title, wtitle, otitle, foatitle,
canonical_title, birth_year, inactive, latest: {standard, rapid, blitz}}`
where each `latest.*` is `{rating, games, k_factor, period}` or `null`.
Unknown → 404. Empty/malformed ID → 400.

### `GET /players/search?q=&federation=&limit=` — read key.
`q` required, ≥ 2 chars (400 otherwise); numeric q → fide_id prefix,
else all-terms name match; `federation` exact (optional); `limit`
default 20, 1–100. Empty result is `200` with `[]` (never 404).
`{data: [...trimmed players with latest...],
pagination: {limit, offset, total}, dataset}`.

### `GET /players/{fide_id}/ratings[?type=&period=]` — read key.
Without `period`: latest snapshot(s) `{data: {fide_id, ratings},
dataset}`. With `period=YYYY-MM`: that period's per-type snapshot(s)
(`null` where absent) plus `"period"` echo. Bad type/period → 400;
unknown player → 404.

### `GET /players/{fide_id}/ratings/history[?type=&from=&to=&limit=&offset=]`
— read key. Ascending entries per type; `limit` default 200, max 1000;
`{data: {fide_id, history}, pagination: {limit, offset}, dataset}`.

### `POST /players/batch` — read key. Body `{"ids": [...]}` (alias
`fide_ids` accepted); 1–100 IDs; invalid entries are skipped into
`missing` (duplicates collapsed). Unknown IDs are reported, not fatal:
`{data: {found: [...], missing: [...]}, dataset}`. Empty/oversize →
400. Non-JSON → 400.

### Admin (separate credential, audit-logged)

- `POST /admin/jobs/ensure` `{"period": "YYYY-MM"}` → 202 + 3 jobs.
- `GET /admin/jobs[?period=&limit=]` (limit ≤ 100) → job states,
  heartbeats, progress, error codes with curated summaries (sanitized:
  `error_message` never carries SQL, paths, or raw exception text —
  full detail stays in server-side worker logs).
- `POST /admin/jobs/retry` `{"period", "rating_type"}` → re-queues one
  FAILED step (completed/active → 400).
- `POST /admin/uploads` (multipart `period`, `rating_type`, `file` .zip)
  → 201 staged; worker prefers staged files over downloading.
- `GET /admin/verify[?period=]` → counts + integrity report
  (duplicates, nulls, invalid types, orphans, periods).

## Non-goals (v1 will NOT add)

Live per-request proxying to ratings.fide.com; player-identity writes
from Swiss; webhooks; GraphQL; per-user auth; response caching (planned
deployment-phase addition, additive under v1).
