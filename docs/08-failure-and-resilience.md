# 08 — Failure and Resilience: Matrix + Per-Consumer Behavior

Principle (INTENDED): FIDE being unavailable must not unnecessarily break
Swiss Manager. Fail closed only where the business rule requires actual
verification; everywhere else degrade gracefully to snapshots, cache, or
empty results. Secrets never in logs.

## 1. Failure matrix (RECOMMENDED FUTURE contract behavior)

| Failure | Signal | Client behavior | Swiss consumer behavior |
|---|---|---|---|
| DNS failure | `FideUnavailableError` (connect) | Raise after timeout+1 retry (GET) | As "unavailable" row per consumer below |
| Connection refused/reset | same | same | same |
| Connect timeout (>3 s) / read timeout (>8 s) | same | same | same |
| `404` player/period | `None` (get) / `[]` (search) / `missing[]` (batch) | **Not** an error — normal miss | Create auto-fill keeps `0/20`; verification request rejects claim (fail closed on miss); profile renders without ratings |
| `401` invalid/missing key | `FideAuthError` | **Fail closed, alert, no fallback** | All consumers raise to error handler; verification/approval refuse; admin health shows `Auth error` (no retry loop) |
| `403` forbidden | `FideAuthError` (scope) | Same as 401 | Same as 401 |
| `429` rate-limited | `FideUnavailableError(rate_limited)` honoring `Retry-After` | Back off; search/history degrade to empty+notice | Auto-fill keeps `0/20`; no busy-retry from request path |
| `5xx` / malformed JSON / schema violation | `FideUnavailableError` | Raise (log `request_id`, never body secrets) | Same as unavailable |
| `503` dataset empty (`DATASET_EMPTY`) | `FideUnavailableError(dataset_empty)` | Raise | Same as unavailable, but admin health shows `Empty` (CURRENT `Available/Empty` badge preserved) |
| Stale cache (period older than N days) | `dataset.period` comparison | Serve with `stale:true` flag + log | Snapshots remain valid; verification approval requires fresh period (fail closed if stale — confirm N in Phase 1) |
| FIDE dataset unavailable at import time | Admin job `failed` + `error_code` | n/a (import is internal to FIDE Service) | Swiss unaffected; status endpoint reports last good period |

Malformed-response rule: any `200` that fails DTO validation is treated as
unavailable (never partially applied). Verification paths additionally fail
closed on malformed.

## 2. Per-consumer expected behavior (CURRENT → FUTURE)

| Consumer | CURRENT | FUTURE (contract §3 mapping) |
|---|---|---|
| C1 search (admin/dashboard/verification boxes) | `[]` on empty/short query; missing ratings → `0` | `[]` + hint on unavailable/rate-limited/invalid; never blocks surrounding page |
| C2 participant-create auto-fill | Silent keep `0/20` on miss | Same on miss **and** unavailable (log `request_id`); auth → raise |
| C3 profile + history | `None`/empty on miss; 403 on unverified URL | Same on miss/unavailable; auth → raise |
| C4 verification request | `ValueError` (Persian) on local miss; duplicate pending/verified blocked | `404`/`None`/unavailable → reject claim (fail closed); auth → raise |
| C5 approval title sync | Empty official → keep existing | Same; unavailable/stale → refuse approval (fail closed) |
| C6 registration dedup | Local-only, creates profile on miss | Unchanged (no FIDE call); optional prefill non-blocking |
| C7 eligibility/pricing | Local `fide_verification_status` / `fide_title` | Unchanged (snapshot-driven) |
| C9 CSV import | Literal columns, row errors | Optional `batch_lookup` warn-and-continue; never block import |
| C11 stats/health | Counts + `Available/Empty` + disk usage | `status` read; `Unknown`/`Empty` badges on failure; pages render |
| C12 standings/print/export | Snapshot-only | Unchanged — never calls FIDE |

## 3. Retry / timeout / cache discipline (RECOMMENDED)

* Timeouts: connect ≤ 3 s, read ≤ 8 s. One retry on idempotent GETs
  (not on `POST /batch`). No retry on `400/401/403/404/429`.
* Backoff: honor `Retry-After` on `429`; otherwise no request-path backoff
  loop — surface and degrade.
* Cache: player/ratings 1 h TTL keyed by `(fide_id)` + dataset period;
  search 60 s bounded LRU; history long TTL per period-range. Never cache
  auth errors; never cache `None` beyond minutes. Cache serves stale only
  with explicit `stale:true` and only on display paths (never verification).
* Snapshots (`rating_snapshot / k_factor / fide_title_snapshot`,
  `profile.fide_title`) are the durable offline cache — tournament flows
  (pairing, standings, prints, exports) must keep working with zero FIDE
  connectivity.
* Logging: `request_id`, endpoint, `fide_id`, latency, outcome. No API keys,
  no full bodies, no PII beyond `fide_id`.
