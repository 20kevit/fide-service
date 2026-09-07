# 07 — Import Architecture: Legacy vs Job-Based, and What Moves

## 1. Two pipelines (CURRENT BEHAVIOR)

### A. Legacy/synchronous — `FideImportOrchestrator`

File: `application/fide/fide_import_orchestrator.py`
(+ facades `application/fide_import_service.py`,
`application/fide_search_service.py`).

* Single `fide_imports` row per period (last-wins), combined XML
  (`FIDE_XML_URL = http://ratings.fide.com/download/players_list_xml.zip`,
  `config.py:60`).
* Entry points: `execute_import / execute_upload_import /
  execute_server_file_import / run_diagnostics / latest_status /
  _save_rating`. Old threaded `execute_import/start_async` removed.
* Serves `POST /admin/fide/upload` (staged `fide_file`) and
  `POST /admin/fide/import/server-file` synchronously.
* `STALE_RUN_MINUTES = 15`, `BATCH_SIZE = 500`, commit-per-batch,
  progress columns (`stage / progress_percent / download_progress /
  processing_progress`), `records_skipped`, `error_category`
  (`FideErrorCategory`), `duration_seconds`.
* Writes **all three** rating types per player per period.

### B. Job-based (preferred) — `FideImportJobService` + CLI worker

Files: `application/fide/fide_job_service.py`,
`infrastructure/models/fide.py:FideImportJobModel`,
`migrations/b3f9c2d74a61_fide_import_jobs.py` (**unapplied**),
`app/cli.py:fide-import-step`,
`interfaces/web/fide_routes.py:trigger_import / retry_job / import_status[jobs]`.

* Persistent 3× `(period, rating_type)` jobs, `UQ(period, rating_type)`.
* `POST /admin/fide/import` only calls
  `ensure_period_jobs(current_period)` and returns immediately (no threads).
* Cron (assumed, every few minutes — no in-repo crontab):
  `flask fide-import-step [--period YYYY-MM] [--timeout SECS]`
  → `tick()` → `fail_stale_jobs` sweep → `claim_next_job` (one step,
  `WITH FOR UPDATE`, strict `standard → rapid → blitz` gating, reasons
  `claimed / busy / stale_present / blocked / failed_present /
  nothing_pending`) → `download → validate → stream-import →
  delete ZIP` → exit (`OK / IDLE / ERROR`).
* Heartbeats on claim, per-10 s during download, per state, per
  `BATCH_SIZE = 500` during parse, on completion. Timeout default
  `FIDE_WORKER_TIMEOUT_SECONDS = 600` (`config.py:78`).
* Stale → `failed / WORKER_STALE / Persian message`; **never auto-restarts —
  explicit `retry_job` only** (`failed → pending`, resets
  timestamps/counters/errors; completed/active raise `ValueError`).
* Per-type archives via `build_rating_url(base, type, period) →
  {type}_{mon}{yy}frl_xml.zip` (e.g. `standard_sep26frl_xml.zip`),
  base `FIDE_DOWNLOAD_BASE_URL` (`config.py:73`).
* Job path writes **only its own** `rating_type`.

### Bridge

Both pipelines upsert the same tables: player merge key `fide_id` (identity
fields follow the latest file), rating merge key
`UQ(fide_id, period, rating_type)` with per-type
`rating / games / k`. Title columns overwritten from
`title / wtitle / otitle / foatitle` (`or ""`).

## 2. Stage-by-stage analysis (CURRENT BEHAVIOR)

| Stage | Legacy | Job-based | Evidence |
|---|---|---|---|
| Download | `download_fide_zip` (timeouts 10/30 s, retries 3, backoff, `.part` + atomic replace, 429/permanent-status taxonomy) | Same, per-type URL + `verify_rating_url` HEAD pre-check + heartbeat per 10 s | `infrastructure/fide/downloader.py:31-239` |
| Validation | `validate_fide_xml / validate_fide_stream` (`REQUIRED_PLAYER_FIELDS`) | Same, per-job `validating` state | `domain/fide/validation.py` |
| Parsing | `parse_fide_xml` streaming `iterparse` + `FIDE_ALLOWED_FEDERATIONS=["IRI"]` filter | Same + `rating_type` isolation | `domain/fide/parser.py:50-86` |
| Batching | `BATCH_SIZE=500`, commit-per-batch | Same, heartbeat per batch | orchestrator `:459-504`, job service `:383-435` |
| Cleanup | `cleanup_old_files` (90-day `YYYY-MM` dirs + husks); success deletes archive, failure keeps for retry; uploads always cleaned (`_remove_staged_quietly`); server-file never deleted on failure | Worker deletes its `<base>/tmp/<YYYY-MM>/<type>.zip` after success (`:344-349`) | `infrastructure/fide/storage.py:36-411` |
| Retry | Sync error → `fide_imports.status=failed` + `error_category`; manual re-trigger | `WORKER_STALE` sweep + explicit `retry_job`; network backoff `sleep(backoff**attempt)` transient-only, permanent + `429/INVALID_CONTENT` fail fast | `repositories/fide.py:186-260`, `downloader.py:118-239` |
| CLI/Cron | None (HTTP-sync) | `flask fide-import-step`; safe concurrent (`busy/blocked`) | `app/cli.py:26-54` |
| Temp files | Period dir `players_list_xml.zip/.xml`, staged `upload_players_list.{zip,xml}` (ext + `secure_filename` + 0-byte + `max_bytes` + ZIP-magic `PK\x03\x04` / XML `<`-start + HTML reject) | `tmp/<YYYY-MM>/<type>.zip` (only `standard\|rapid\|blitz`), `.part` during download | `storage.py:63-90,143-411` |

## 3. Duplicated logic (CURRENT — consolidate in FIDE Service)

1. Two import entry points (orchestrator vs job service) sharing
   parser/validation/storage/downloader but duplicating upsert + progress
   + error mapping. 2. Two status models (`fide_imports` last-wins row vs
   `fide_import_jobs` per-step rows) with overlapping vocabularies
   (`status: pending/success/failed` vs `state:
   pending/downloading/validating/importing/completed/failed`).
3. Two title/rating write paths (`_save_rating` vs `_upsert_player`).
4. Facade shims (`fide_import_service.py`, `fide_search_service.py`) kept
   for compat.

## 4. Recommendation (RECOMMENDED FUTURE — belongs in FIDE Service)

* Keep the **job-based pipeline as the sole pipeline**. Retire the
  orchestrator's combined-XML path after a cutover period; keep staged
  upload + server-file ingestion as job types (or one-shot admin jobs),
  not separate sync code paths.
* Move verbatim: `domain/fide/{parser, validation, errors, models}`,
  `infrastructure/fide/{storage, downloader, diagnostics}`,
  `FideImportJobService` state machine + `claim/fail_stale/retry`
  repository logic, `FideImportJobModel` + `FideRatingModel` /
  `FidePlayerModel` schemas, `fide-import-step` worker semantics
  (renamed CLI, e.g. `flask fide-worker --period …` or Cron hitting an
  admin endpoint — decide in Phase 1, see `12-open-decisions.md`).
* Unify status: jobs are the execution truth; `fide_imports` becomes a
  derived per-period rollup/history (or is retired in favor of
  `GET /admin/imports` aggregation). Confirm vocabulary in Phase 1.
* Keep invariants: streaming-only (never extract ~847 MB XML), ZipSlip-safe
  member access, atomic `.part` replace, idempotent upserts, heartbeat
  protocol, explicit-retry-only, success-deletes-archive /
  failure-keeps-for-retry, 90-day file retention, `BATCH_SIZE=500`
  commit-per-batch until re-benchmarked.
* Swiss keeps **no** import code, no `FIDE_DATA_DIR`, no Cron for FIDE.
  Uploads (if still needed) go to the FIDE Service admin surface, never
  through Swiss.
