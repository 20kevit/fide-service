# 11 — Extraction Plan: FIDE Inside Swiss → Independent FIDE Service

Phases are large vertical slices — future coding agents execute substantial
end-to-end steps. Each phase lists objective, prerequisites, affected
systems, data movement, implementation scope, tests, acceptance criteria,
rollback, and production risk. Git discipline: separate repos, small
focused independently-revertible commits, inspect tree + staged diff before
every commit, never commit secrets/`.env`/venvs/caches/generated files.
No phase rewrites existing history or discards user work.

## Phase 1 — FIDE Service skeleton + ported domain/infrastructure (no traffic)

* Objective: standalone Flask app with FIDE-only schema, ported parser/
  validation/errors/storage/downloader/diagnostics + job/search services,
  passing domain/importer/repository tests on a scratch DB.
* Prerequisites: this Phase 0 workspace committed; `[CONFIRM]` host/venv/DB
  target; `12-open-decisions.md` BLOCKERs triaged (at minimum: migration
  approval for `b3f9c2d74a61`, API-key header/scope decision).
* Affected: `fide_service` only. Swiss untouched.
* Data movement: none (synthetic + fixture XML only).
* Scope: app factory, config (env-only secrets), FIDE models/migrations
  (seed §4 of `04-database.md`), repositories, job service (sole pipeline),
  search service, CLI worker (`fide-worker`), `/healthz` + `/api/v1/status`
  stub. Retire orchestrator duplicates at port time (keep upload/server-file
  as job types).
* Tests: `10-testing-strategy.md` rows domain/parser/importer/repository/
  model-migration/upstream (mocked).
* Acceptance: `pytest` green on FIDE-only DB; migration chain applies clean
  on scratch MySQL + SQLite; worker `tick()` fixture run completes with
  heartbeats.
* Rollback: delete scratch resources (no prod surface).
* Risk: low (no traffic, no Swiss change).

## Phase 2 — Read API v1 + admin separation + seed import

* Objective: implement `05-api-contract.md` §§1–7 with auth, error envelope,
  pagination, `dataset` block, cache headers; run a real one-period import.
* Prerequisites: Phase 1 green; admin-scope decision; API-key issuance flow.
* Affected: `fide_service` only.
* Data movement: one full period import (per-type archives) into the FIDE
  DB; record actual row counts + query plans (feeds `04-database.md` §7).
* Scope: read blueprints, key auth (`hmac.compare_digest`, fail closed),
  rate-limit baseline, admin endpoints (separate scope/key, audit log),
  ETag/`Cache-Control`, diagnostics surface.
* Tests: API contract + resilience (mocked faults) + status/health.
* Acceptance: contract tests green; manual `curl` matrix (200/400/401/404/
  429/503) verified; `status` shows the seeded period; logs contain no
  secrets.
* Rollback: re-seed or wipe FIDE DB (no consumers yet).
* Risk: low-medium (real upstream dependency; retries/backoff verified).

## Phase 3 — Swiss client + shadow/dual-read (no cutover)

* Objective: build `06-swiss-client-contract.md` client in Swiss
  (`application/fide_client/`) with DTOs/errors/cache/fallback; run it in
  shadow mode alongside local reads (compare + log diffs, serve local).
* Prerequisites: Phase 2 staging endpoint + key; Swiss-side fix design for
  the `player_edit.html` bug (fix itself lands here or Phase 4 — confirm).
* Affected: Swiss `application/fide_client` (new), logging/metrics glue.
* Data movement: none (reads only, shadow).
* Scope: client, per-consumer call sites behind flags (C1–C5, C11), diff
  logging with `request_id`, timeout/retry/circuit discipline.
* Tests: client unit tests + consumer shadow tests; snapshot-guard tests
  proving pairing/standings/prints/exports make zero HTTP calls.
* Acceptance: shadow diff rate ~0 on IRI players for search/get/ratings/
  history/batch; all diffs triaged (parser vs data vs mapping).
* Rollback: disable flags (single commit revert per consumer).
* Risk: medium (first Swiss-side change; strict review, no behavior change
  while flagged off).

## Phase 4 — Cut over reads per consumer (flagged, reversible)

* Objective: switch C1→C5, C11 from local FIDE tables to the service,
  keeping snapshots as fallback; fix the `player_edit.html` wipe bug.
* Prerequisites: shadow parity; stale-period threshold N decided;
  verification fail-closed semantics confirmed.
* Affected: Swiss routes/services C1–C5, C8 (edit fix), C11; docs updated.
* Data movement: none (Swiss stops reading its `fide_*` tables; tables
  retained but frozen).
* Scope: flag-on per consumer with per-consumer fallback (table in
  `06-swiss-client-contract.md` §3); edit-form fix (inputs or hidden
  preserves + full title list, or `update()` ignores missing keys) +
  regression test (fails today).
* Tests: consumer tests under ok/miss/unavailable/auth-fail stubs;
  edit-bug regression; full Swiss suite.
* Acceptance: each consumer works with service up, degraded-but-correct
  with service down (except fail-closed verification), and correct on
  `404`/stale; edit round-trip preserves `fide_id`/`k_factor`/titles.
* Rollback: per-consumer flag revert (independently revertible commits).
* Risk: high (user-facing). Roll out C1/C11 first, C2/C3 next, C4/C5 last
  (fail-closed paths get extra review + manual QA).

## Phase 5 — Decommission Swiss FIDE write paths + data migration

* Objective: retire Swiss import routes/CLI/storage/migrations for FIDE;
  migrate historical `fide_players`/`fide_ratings` into the service DB;
  Swiss `fide_*` tables become read-frozen then dropped in a later release.
* Prerequisites: Phase 4 stable in production for ≥1 import cycle;
  migration rehearsal on a copy (row counts, checksums per
  `(fide_id, period, rating_type)`, title-column verbatim check).
* Affected: Swiss (remove `fide_routes` import endpoints, `fide-import-step`
  Cron, `FIDE_DATA_DIR`, FIDE migrations) + FIDE Service (bulk seed).
* Data movement: bulk export/import of `fide_players` + `fide_ratings`
  (idempotent upserts); `fide_imports` history archived, not migrated
  row-for-row (confirm in Phase 1); `fide_import_jobs` fresh (no migration).
* Scope: Swiss cleanup commits (small, one surface per commit), migration
  scripts (side-loaded, reviewed, never `reset_db`), dual-run until parity.
* Tests: migration checksum tests; Swiss suite with FIDE tables absent;
  service suite with migrated data.
* Acceptance: checksums match; service serves history migrated from Swiss;
  Swiss suite green without `fide_*` tables; Cron/logs show zero Swiss-side
  FIDE worker activity.
* Rollback: keep a restorable dump of Swiss `fide_*` tables + the
  decommission commits revertible in reverse order; service can re-seed
  from upstream regardless.
* Risk: high (data movement). Rehearse twice; freeze imports during the
  window; back up both DBs immediately before.

## Phase 6 — Hardening + independent operations

* Objective: production-grade operations: rate limits tuned from measured
  load, backup/restore rehearsed, monitoring/alerting (failed/`WORKER_STALE`
  jobs, `401` spikes, staleness), runbooks, load/Soak of import + API.
* Prerequisites: Phases 1–5 done; `[CONFIRM]` prod paths/quotas.
* Scope: dashboards, alerts, retention enforcement, secret rotation
  procedure, admin audit review, doc refresh (`09-deployment.md`,
  `08-failure-and-resilience.md`).
* Acceptance: restore rehearsal succeeds; chaos checks (kill worker mid-job
  → `WORKER_STALE` → explicit retry completes); key rotation without
  downtime; docs match reality.
* Rollback: config-only changes, revertible per change.
* Risk: medium (operational, no schema change).
