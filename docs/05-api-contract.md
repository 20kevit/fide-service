# 05 — API Contract: REST API v1 (Draft for Implementation)

Derived from actual Swiss consumers (see `docs/02-dependency-map.md`).
Do not overengineer: v1 covers exactly what C1–C5 + C11 need, plus
separated protected import/admin reads. Base path `/api/v1`.

## 0. Cross-cutting rules

* **Transport:** HTTPS only in production. (Note: current Swiss config uses
  `http://ratings.fide.com/download/` as the *upstream* base — the new
  service-to-service boundary is HTTPS regardless; see `12-open-decisions.md`.)
* **Auth:** service-to-service API key (INTENDED direction). Client sends
  `X-API-Key: <key>` (exact header name is RECOMMENDED, confirm in Phase 1);
  server compares with `hmac.compare_digest`, fail closed (401 on
  missing/invalid, no fallback to anonymous). Key from environment only,
  never in logs/URLs/error bodies. No session cookies, no user RBAC on the
  read API. Rate limiting: RECOMMENDED (e.g. per-key token bucket) but
  unspecified until load is measured — at minimum return `429` with
  `Retry-After` when enforced.
* **Content:** `application/json; charset=utf-8`. Request/response bodies
  are JSON objects (never bare arrays).
* **Error format (all errors):**
  ```json
  { "error": { "code": "NOT_FOUND", "message": "Player 1234567 not found",
               "details": {}, "request_id": "01J..." } }
  ```
  `code` is a stable machine string (`BAD_REQUEST`, `UNAUTHORIZED`,
  `FORBIDDEN`, `NOT_FOUND`, `RATE_LIMITED`, `UPSTREAM_UNAVAILABLE`,
  `DATASET_EMPTY`, `INTERNAL`). `message` is human-readable and
  secret-free. `request_id` correlates with server logs.
* **Status codes:** `200` ok (incl. empty search), `400` validation,
  `401` missing/invalid key, `403` forbidden (reserved for future scopes),
  `404` unknown player/period, `429` rate-limited, `502` upstream/import
  failure surfaced through admin reads, `503` dataset empty/unavailable,
  `5xx` never leaks tracebacks.
* **Pagination:** cursor- or offset-based with `limit` (default 20, max 100)
  + `offset`; responses include `{ "data": [...], "pagination": { "limit": 20, "offset": 0, "total": 123 } }`
  where `total` may be omitted for expensive queries (document per endpoint).
* **Timeouts (RECOMMENDED):** server handler budget ≤ 5 s for reads;
  Swiss client timeout ≤ 3 s connect / ≤ 8 s read with one retry on
  idempotent GETs only (see `08-failure-and-resilience.md`).
* **Cache semantics:** `ETag` + `Last-Modified` on player/ratings GETs;
  `Cache-Control: public, max-age=86400` for immutable history periods,
  `max-age=3600` for latest/period-current reads; search is
  `no-store` (or short `max-age=60`). Every player/ratings response carries
  dataset versioning (see §6).
* **Dataset versioning (REQUIRED on every data response):**
  ```json
  "dataset": { "period": "2026-09", "imported_at": "2026-09-03T02:14:00Z",
               "source": "fide-official-xml", "rating_type": "standard" }
  ```
  `period` is `YYYY-MM`; history items carry their own `period`.

## 1. `GET /api/v1/players/{fide_id}` — get player

Replaces: C3 (profile), C4 (existence check), C5 (official titles).

* Auth: API key. Path: `fide_id` = 1–20 char FIDE ID (trim; reject empty).
* Query: `?include=ratings` (optional; embeds latest §3 payload).
* Response `200`:
  ```json
  { "data": { "fide_id": "12512294", "name": "…", "federation": "IRI",
              "sex": "M", "title": "GM", "wtitle": "", "otitle": "",
              "foatitle": "", "birth_year": "1999", "inactive": false,
              "latest": { "standard": {"rating": 2612, "games": 9, "k_factor": 10, "period": "2026-09"},
                          "rapid": null, "blitz": null } },
    "dataset": { "period": "2026-09", "imported_at": "…" } }
  ```
  (`latest.*` is `null` when no row for that type; `include=ratings`
  controls only the embed — the canonical latest endpoint is §3.)
* Errors: `400` malformed id, `401`, `404` unknown id, `503` dataset empty.

## 2. `GET /api/v1/players/search` — search players

Replaces: C1 (admin + dashboard + verification search boxes).

* Auth: API key. Query: `q` (required, ≥2 chars; numeric → `fide_id`
  prefix, else per-word name substring — mirrors
  `FidePlayerRepository.search_players`), `federation` (optional, e.g.
  `IRI`; CURRENT admin default is `IRI`, dashboard passes none),
  `limit` (default 20, max 100), `offset` (default 0),
  `include_ratings=true|false` (default true — current service bulk-loads
  latest ratings in one query).
* Response `200`: `{ "data": [ {player as in §1, trimmed} ], "pagination": {...}, "dataset": {...} }`.
  Empty query/short query → `200` with `[]` (CURRENT behavior returns `[]`,
  not `400` — but v1 REQUIRES `q`; `<2 chars` → `400` with
  `code=BAD_REQUEST`. Confirm the length floor in Phase 1.)
* Errors: `400` missing/short `q` or bad `limit`, `401`, `429`, `503`.

## 3. `GET /api/v1/players/{fide_id}/ratings` — latest ratings

Replaces: C2 (auto-fill), C3 (profile latest).

* Auth: API key. Query: `?type=standard|rapid|blitz` (optional; omit →
  return all three).
* Response `200`:
  ```json
  { "data": { "fide_id": "…",
              "ratings": { "standard": {"rating": 2612, "games": 9, "k_factor": 10, "period": "2026-09"},
                           "rapid": null, "blitz": null } },
    "dataset": { "period": "2026-09", "imported_at": "…" } }
  ```
* Errors: `400` bad type, `401`, `404` unknown player, `503`.

## 4. `GET /api/v1/players/{fide_id}/ratings/history` — rating history

Replaces: C3 history chart (`get_rating_history`, grouped asc).

* Auth: API key. Query: `?type=standard|rapid|blitz` (optional),
  `?from=YYYY-MM&to=YYYY-MM` (optional, inclusive),
  `?limit` (default 120, max 1200) + `?offset`.
* Response `200`:
  ```json
  { "data": { "fide_id": "…",
              "history": { "standard": [{"period":"2026-06","rating":2601,"games":9,"k_factor":10}],
                           "rapid": [], "blitz": [] } },
    "pagination": {"limit":120,"offset":0}, "dataset": {"period":"2026-09","imported_at":"…"} }
  ```
* Errors: `400` bad period/type, `401`, `404`, `503`.

## 5. `POST /api/v1/players/batch` — batch lookup

Replaces: C1 bulk path (`get_by_fide_ids`, `get_latest_for_fide_ids`),
C9 optional CSV validation, reactivation prefill.

* Auth: API key. Body: `{ "fide_ids": ["…", …], "include_ratings": true }`
  (max 100 ids; duplicates tolerated; unknown ids reported, not fatal).
* Response `200`:
  ```json
  { "data": { "found": [ {player as in §1} ], "missing": ["…"] },
    "dataset": { "period": "2026-09", "imported_at": "…" } }
  ```
* Errors: `400` empty/too many ids, `401`, `413` oversize, `429`, `503`.

## 6. `GET /api/v1/status` — service + dataset status

Replaces: C11 (dashboard stats card, health badge).

* Auth: API key (or separate monitoring key — RECOMMENDED: same key,
  document in Phase 1). Response `200`:
  ```json
  { "data": { "ok": true, "players": 12345, "periods": ["2026-07","2026-08","2026-09"],
              "latest_period": "2026-09", "last_import": {"period":"2026-09","status":"success","imported_at":"…"},
              "jobs": {"pending":0,"failed":0} },
    "dataset": { "period": "2026-09", "imported_at": "…" } }
  ```
* `GET /api/v1/healthz` (unauthenticated, load-balancer only):
  `{ "ok": true }` with no dataset details.

## 7. Import/admin endpoints (separated, protected — not part of the public read surface)

CURRENT import is triggered/polled via admin HTML + `fide_imports`/`jobs`
tables. RECOMMENDED v1 admin surface (exact shape confirmed in Phase 1):

```text
POST /api/v1/admin/imports                (body: {period, types[]}) → 202 + job ids
GET  /api/v1/admin/imports?period=YYYY-MM (history = fide_imports rows)
GET  /api/v1/admin/jobs?period=YYYY-MM    (job rows incl. state/heartbeat/error)
POST /api/v1/admin/jobs/retry             (body: {period, rating_type})
GET  /api/v1/admin/diagnostics            (connectivity ladder payload)
```

Rules: separate auth scope/key from read API (RECOMMENDED: distinct
`X-Admin-Key` or scoped key); never exposed to tournament users; rate-limit
strictly; audit-log every mutation. The admin API is versioned independently
of the read API.

## 8. Non-goals for v1

No live proxy to `ratings.fide.com` per request; no write access to player
identity from Swiss (verification workflow state stays Swiss-side); no
webhook fan-out; no GraphQL; no per-user auth. Swiss writes nothing into
the FIDE dataset — the boundary is read-only except for the separated
admin surface.
