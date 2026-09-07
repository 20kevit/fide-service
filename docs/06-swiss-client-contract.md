# 06 — Swiss Client Contract: What Swiss May Depend On

INTENDED: after extraction, Swiss Manager must never import FIDE ORM
models, FIDE repositories, the FIDE database, or the FIDE filesystem.
It depends only on this contract — a thin HTTP client plus DTOs with
documented failure/fallback semantics.

## 1. Client shape (RECOMMENDED FUTURE)

```text
application/fide_client/
├── client.py        # FideServiceClient (HTTP, timeouts, retries, auth header)
├── dto.py           # frozen dataclasses: FidePlayerDTO, LatestRatingsDTO,
│                    #   RatingPointDTO, SearchResultDTO, DatasetInfoDTO
├── errors.py        # FideServiceError hierarchy (mirrors API error codes)
├── cache.py         # short-TTL search cache (opt-in, bounded)
└── fallback.py      # snapshot/fallback helpers used by consumers
```

Rules: the client lives in `application/` (may own no transactions beyond
its own cache writes); it must not import `infrastructure.models.fide` or
`infrastructure.repositories.fide`; every method documents input/output/
failures/fallback/caching (tables below).

## 2. Operations

### `get_player(fide_id, include_ratings=False) -> FidePlayerDTO | None`

* Input: `fide_id: str` (trim; `""` → `None` without HTTP call).
* Output DTO: `{fide_id, name, federation, sex, title, wtitle, otitle,
  foatitle, birth_year, inactive, latest: {standard, rapid, blitz} | None,
  dataset: DatasetInfoDTO}`.
* Failures: `404` → `None`; `401/403` → raise `FideAuthError` (fail closed,
  alert, no silent fallback); timeout/connection/`5xx`/malformed →
  raise `FideUnavailableError`.
* Fallback: caller decides (table §3). Verification request path treats
  `None`/unavailable as fail-closed rejection; display paths degrade to
  local data.
* Caching: short TTL (e.g. 1 h) keyed by `fide_id`, invalidated on new
  dataset period; never cache `None` longer than minutes.

### `search_players(query, federation=None, limit=20) -> SearchResultDTO`

* Input: `query` (require `len ≥ 2` client-side; shorter → `[]` without
  HTTP call, mirroring CURRENT dashboard behavior), `federation`
  (admin UI passes `"IRI"` today; dashboard passes `None`),
  `limit ≤ 100`.
* Output: list of `FidePlayerDTO` (trimmed) + `dataset`.
* Failures: `400` → `[]` + log; auth → raise; unavailable/malformed →
  `[]` + surfaced notice (search is non-critical).
* Fallback: empty results with UI hint; never block the surrounding flow.
* Caching: opt-in 60 s bounded LRU for identical `(q, federation, limit)`.

### `get_ratings(fide_id, rating_type=None) -> LatestRatingsDTO | None`

* Input: `fide_id`, optional `type ∈ {standard, rapid, blitz}`.
* Output: `{fide_id, ratings: {standard, rapid, blitz} (each
  `{rating, games, k_factor, period}` or `None`), dataset}`.
* Failures: `404`/no rows → DTO with `None`s (CURRENT `get_latest_rating`
  returns `None` → caller keeps `0/20`); auth → raise; unavailable →
  raise `FideUnavailableError`.
* Fallback: participant-create auto-fill keeps `0/20` (CURRENT silent
  fallback preserved); explicit resync surfaces the error.
* Caching: same TTL as `get_player`.

### `get_rating_history(fide_id, rating_type=None, from=None, to=None) -> RatingHistoryDTO`

* Output: `{fide_id, history: {standard: [...asc], rapid: [...], blitz: [...]}, dataset}`.
  No `fide_id` → empty DTO without HTTP call (CURRENT behavior).
* Failures: unavailable → empty DTO + chart notice; auth → raise.
* Caching: long TTL per `(fide_id, period-range)`; history periods are immutable.

### `batch_lookup(fide_ids, include_ratings=True) -> BatchResultDTO`

* Input: ≤ 100 ids (chunk larger sets client-side).
* Output: `{found: [FidePlayerDTO], missing: [str], dataset}`.
* Failures: partial success is normal (`missing` list); auth → raise;
  unavailable → raise (CSV validation treats as skip-with-warning, never block).

## 3. Consumer migration (CURRENT → contract)

| Consumer (CURRENT) | Contract call | Fallback on `FideUnavailableError` |
|---|---|---|
| C1 search boxes | `search_players` | `[]` + hint; flow continues |
| C2 participant-create auto-fill | `get_ratings` | Keep `rating 0 / k 20` silently (CURRENT) |
| C3 profile + history | `get_player` / `get_rating_history` | Local profile renders; ratings `None`/chart empty |
| C4 verification request | `get_player` | **Fail closed:** reject claim (CURRENT `ValueError` preserved) |
| C5 approval title sync | `get_player` (official titles) | **Fail closed:** do not approve on stale/missing data |
| C6 registration dedup | No FIDE call (local `player_profiles`) | n/a (optional prefill via `get_player`, non-blocking) |
| C9 CSV import | `batch_lookup` (optional validate) | Warn + continue with literal values |
| C11 stats/health | `GET /status` | `Unknown` badge; admin pages render |

## 4. Invariants the client enforces (INTENDED)

1. **No silent wipe:** `fide_id`/`k_factor` updates require explicit
   caller intent; missing keys ≠ `""`/`20`. (Direct response to the
   `player_edit.html` bug.)
2. **Snapshot-first:** pairing, standings, prints, exports read Swiss
   snapshots only — never call the client in those paths.
3. **Fail-closed verification:** any auth failure or unverifiable response
   rejects the verification step; secrets never logged.
4. **Secret hygiene:** API key from environment, injected at client
   construction; redacted in `repr`/logs/errors.
5. **Timeout discipline:** connect ≤ 3 s, read ≤ 8 s, one retry on
   idempotent GETs, no retry on POST batch; circuit-break or degrade after
   consecutive failures (see `08-failure-and-resilience.md`).
