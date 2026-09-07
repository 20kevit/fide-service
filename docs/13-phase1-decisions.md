# 13 — Phase 1 Build Decisions (Local Implementation)

How the Phase 1 service resolves the `12-open-decisions.md` items, what was
deliberately changed vs the Swiss source, and what stays open. Phase 0
docs are NOT rewritten; this file is the Phase 1 delta record.

## A. Blocker resolutions (Phase 0 §BLOCKER)

1. **Migration approval (item 20).** RESOLVED for local scope: the new
   service owns a fresh migration history (`migrations/versions/
   v0001_initial.py` creating `fide_players`, `fide_ratings`,
   `fide_import_jobs` via ORM metadata, tracked in `schema_migrations`).
   No existing database was touched — the local DB (`sqlite:///fide.db`)
   is newly created by `flask --app run.py db-upgrade`. The stale Swiss
   `b3f9c2d74a61` question is moot for this repo: Swiss migrations were
   neither run nor copied as a dependency.
2. **Swiss edit-form data loss (item 21).** NOT FIXED (out of scope by
   design) — recorded as a separate Swiss Manager task below (§D). The
   new service does not depend on, mirror, or work around the bug: the
   API is read-only for identity/ratings, so no Swiss write path can
   corrupt FIDE data through it.
3. **API-key issuance/storage/rotation (item 22).** RESOLVED for local
   scope: `FIDE_API_KEY` (reads) + optional `FIDE_ADMIN_KEY` (admin;
   falls back to the read key when unset) from environment only, compared
   with `hmac.compare_digest`, fail closed (401; 503 when unconfigured).
   Rotation = change env + restart (documented in README). Server-side
   issuance workflow for production remains a deployment-phase FOLLOW-UP.
4. **Swiss table freeze/drop rehearsal (item 23).** NOT APPLICABLE in
   Phase 1 (no data movement, no cutover). Carried to the migration phase.

## B. RECOMMENDED confirmations (Phase 0 §RECOMMENDED)

5. **Headers/envelope (item 9).** CONFIRMED with one addition: admin also
   accepts `X-Admin-Key` (falls back to `X-API-Key`); envelope is exactly
   `{error: {code, message, details, request_id}}`; every data payload
   carries `dataset: {period, imported_at, source}`.
6. **Sole job pipeline (item 10).** CONFIRMED + extended: the legacy
   orchestrator was NOT ported. Uploads stage a ZIP that the worker
   prefers over downloading (provenance reported as `staged` vs
   `downloaded`). `fide_imports` was NOT recreated — jobs are the only
   execution truth; `GET /status` derives the rollup on read.
7. **Timeouts/cache (item 11).** Server-side timeouts live on the
   downloader/worker (connect/read/retry env). HTTP response caching is
   NOT implemented in Phase 1 (no Cache-Control/ETag yet) — FOLLOW-UP
   for the deployment phase; the stateless API is cheap without it at
   local scale.
8. **Swiss client (item 12).** DEFERRED by task order (client + cutover
   are later phases). The API is shaped exactly to the documented client
   operations (get/search/latest/history/batch/status).
9. **Migrations/Python/worker (item 13).** CONFIRMED as built: owned
   history, local Python 3.14 venv (prod target stays 3.12 per Phase 0),
   worker command `fide-worker` (flag `--period/--timeout`).

## C. UNKNOWN resolutions (Phase 0 §UNKNOWN)

10. **Search floor/total (item 14).** DECIDED: `q` required, min 2 chars
    (400 otherwise); `limit` default 20, max 100; response includes the
    bounded `total` (= returned count). Federation optional (no default).
11. **Staleness N + status auth (item 15).** DECIDED: no staleness gate
    inside the service — every payload carries its dataset period and
    verification freshness stays a Swiss-side rule. `/status` uses the
    read key; `/healthz` is open.
12. **birth_year form (item 16).** DECIDED: preserved verbatim as
    `String(16)` (official `<birthday>` text, never truncated to a year).
    Empty-official-title behavior is a Swiss verification rule and does
    not exist in this service (raw columns only + derived
    `canonical_title`).
13. **Production paths/quota (item 17).** Still `[CONFIRM]` — untouched,
    deployment phase owns it.
14. **Upstream HTTP base (item 18).** DECIDED: outbound downloads keep the
    official `http://ratings.fide.com/download/` base (outbound fetch
    only); the service-to-service API contract is HTTPS at deployment.
    No translation/proxy invented.
15. **Row counts/plans (item 19).** Still unmeasured at scale (local
    fixtures only). `fide-verify` + `admin/verify` report counts,
    per-type counts, duplicates, nulls, invalid types, orphans, periods —
    ready for the September-baseline rehearsal (32,925 players / 55,033
    snapshots reference, `docs` Phase 0 §16).

## D. Separate Swiss Manager task (do NOT do here)

- **SWISS-TASK-1 — participant edit data loss:** `player_edit.html`
  exposes neither `fide_id` nor `k_factor` (and only GM/IM/FM titles)
  while `ParticipantManagement.update` overwrites all three from the
  form. Fix on the Swiss side (inputs or hidden preserves + full title
  list, or ignore-missing-keys) with a regression test. Phase: cutover
  prep at the earliest.
- **SWISS-TASK-2 — legacy pipeline retirement:** the legacy synchronous
  importer stays live in Swiss because Beta works. Retire only after the
  service cutover + acceptance (later phase).

## E. Deliberate divergences from the Swiss source

- `verifying` job state added (counts reconcile before ZIP deletion).
- `birth_year String(16)` instead of `String(4)` (no silent truncation).
- English operator messages (JSON API, no Persian admin UI); category
  names unchanged.
- Latest-per-type resolved in SQL (`GROUP BY` max period), never in Python.
- Same-state heartbeat is a no-op; `expunge_all()` re-attaches the job
  row (both found by tests, see commit `be643d9`).
- `validate_stream` treats `size_bytes=0` as unknown (raw streams), not
  empty — empty content still fails the head check.
- No `fide_imports` table, no facades, no Flask in domain/application,
  no `current_app` below the interfaces layer (config injected).

## F. Migration report (Phase 1)

- Files: `migrations/versions/v0001_initial.py` (creates all three
  tables + indexes + constraints, idempotent).
- Runner: `infrastructure/migrations.py` + `schema_migrations` table;
  CLI `db-upgrade` / `db-status`.
- Database: newly created local SQLite file (never an existing DB).
- Downgrades: intentionally unsupported in Phase 1 (restore from backup).

## G. Open items (honest carry-overs)

- FOLLOW-UP: response caching (ETag/Cache-Control), tuned rate limits,
  secret rotation runbook, production paths/quota, full-scale import
  rehearsal + query plans, HTTPS deployment of `fide.chessmanager.ir`.
- RISK: SQLite `with_for_update()` is a no-op — atomicity holds for the
  single local worker; MySQL/Postgres must be verified before any
  multi-worker deployment.
- BLOCKER (later phase): production key issuance, Swiss table freeze/drop
  rehearsal, `SWISS-TASK-1` before cutover.
