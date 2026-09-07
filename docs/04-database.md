# 04 — Database: FIDE Tables Today and What Moves

Source: `infrastructure/models/fide.py` (102 lines, read fully).
Migration chain: `migrations/versions/` (head `b3f9c2d74a61`).
Charset: `utf8mb4 / utf8mb4_unicode_ci` on all FIDE tables.

## 1. `fide_players` — `FidePlayerModel` (`infrastructure/models/fide.py:8-23`)

> "FIDE player identity data (global ready, filtered by config during import)."

| Column | Type | Nullable / default | Key / index |
|---|---|---|---|
| `fide_id` | `String(20)` | PK (→ NOT NULL) | PRIMARY KEY (unique by definition) |
| `name` | `String(200)` | NOT NULL | — |
| `sex` | `String(1)` | default `"M"` (DDL-nullable, app default) | — |
| `federation` | `String(5)` | default `""` | `ix_fide_players_federation` |
| `title` | `String(10)` | default `""` | — |
| `wtitle` | `String(10)` | default `""` | — |
| `otitle` | `String(10)` | default `""` | — |
| `foatitle` | `String(10)` | default `""` | — |
| `birth_year` | `String(4)` | default `""` (stores `<birthday>` text verbatim) | — |
| `inactive` | `Boolean` | default `False` | — |
| `updated_at` | `DateTime` | default `utcnow`, `onupdate=utcnow` | — |

Relationships: none declared. Target of one FK (`fide_ratings.fide_id`).
Baseline migration: `bb0160eefd6b` lines 25-64.

## 2. `fide_ratings` — `FideRatingModel` (`infrastructure/models/fide.py:26-40`)

> "Monthly rating history for a FIDE player."

| Column | Type | Nullable / default | Key / index |
|---|---|---|---|
| `id` | `Integer` | PK | PRIMARY KEY |
| `fide_id` | `String(20)` + `FK("fide_players.fide_id")` | NOT NULL | FK + `ix_fide_ratings_fide_id` |
| `period` | `String(7)` (`YYYY-MM`) | NOT NULL | `ix_fide_ratings_period` |
| `rating_type` | `String(10)` (`standard\|rapid\|blitz`) | NOT NULL | part of UQ |
| `rating` | `Integer` | default `0` | — |
| `games` | `Integer` | default `0` | — |
| `k_factor` | `Integer` | default `20` | — |

Constraint: `UQ(fide_id, period, rating_type)` named
`uq_fide_rating_period_type`. The **only FK** among FIDE tables.

## 3. `fide_import_jobs` — `FideImportJobModel` (`infrastructure/models/fide.py:43-74`)

> Persistent per-type WORK tracker — tracks work, not data. One row per
> `(period, rating_type)`.

| Column | Type | Nullable / default |
|---|---|---|
| `id` | `Integer` | PK |
| `period` | `String(7)` | NOT NULL, indexed |
| `rating_type` | `String(10)` | NOT NULL |
| `state` | `String(20)` | NOT NULL default `"pending"` (`pending\|downloading\|validating\|importing\|completed\|failed`) |
| `started_at` / `heartbeat_at` / `completed_at` | `DateTime` | nullable |
| `records_processed` | `Integer` | default `0` |
| `total_records` | `Integer` | nullable (`NULL` = unknown, never faked) |
| `error_code` | `String(40)` | nullable (`FideErrorCategory` + `WORKER_STALE`) |
| `error_message` | `Text` | nullable |
| `source_url` / `file_name` | `String(255)` | nullable |

Constraints: `UQ(period, rating_type)` (`uq_fide_job_period_type`),
`Index(ix_fide_import_jobs_state, state)` + `ix_..._period`.
Migration `b3f9c2d74a61` (table-exists guard; **NOT YET APPLIED anywhere**
per its header — explicit approval required before `flask db upgrade`).

## 4. `fide_imports` — `FideImportModel` (`infrastructure/models/fide.py:77-102`)

> One row per period (last-wins). Operation history for diagnosability.

| Column | Type | Nullable / default |
|---|---|---|
| `id` | `Integer` | PK |
| `period` | `String(7)` | NOT NULL, `ix_fide_imports_period` |
| `source_url` | `String(255)` | nullable |
| `downloaded_at` | `DateTime` | default `utcnow` |
| `imported_at` | `DateTime` | nullable |
| `status` | `String(20)` | default `"pending"` (`pending\|success\|failed`) |
| `records_processed` / `records_imported` | `Integer` | default `0` |
| `error_message` | `Text` | nullable |
| `stage` | `String(20)` | nullable (`download\|extract\|parse\|finalize`, + `validate\|upload`) |
| `progress_percent` / `download_progress` / `processing_progress` | `Integer` | nullable (0..100, `NULL` = indeterminate) |
| `file_name` | `String(255)` | nullable (added `e7a2b4c91f05`) |
| `error_category` | `String(40)` | nullable (added `e7a2b4c91f05`) |
| `records_skipped` | `Integer` | default `0` (added `e7a2b4c91f05`, backfilled `0`) |
| `duration_seconds` | `Integer` | nullable (added `e7a2b4c91f05`) |

## 5. Adjacent Swiss tables (do NOT move — reference only)

* `player_profiles.fide_id String(20) nullable indexed` (no FK, **not**
  unique), `fide_title String(5) default ""`,
  `fide_verification_status default "unverified"`
  (`infrastructure/models/profile.py:14`).
* `tournament_participants.fide_title_snapshot String(5)`,
  `k_factor Int default 20`, `rating_snapshot`
  (`infrastructure/models/participant.py:24-25`).
* `player_verifications.requested_fide_id String(20)` NOT NULL (no FK);
  FKs only to `player_profiles.id` and `users.id`
  (`infrastructure/models/verification.py`).

## 6. Migration history (CURRENT BEHAVIOR)

| File | Revises | Effect |
|---|---|---|
| `bb0160eefd6b_fresh_baseline_schema_clean_production_.py` | `None` | Squashed baseline: creates `fide_imports` (12 cols), `fide_players` (+ federation index), `fide_ratings` (FK + UQ + 2 indexes). |
| `c41a9e2b07d3_beta_profile_role_requests.py` | `bb0160…` | No FIDE change. |
| `f3d8a1c47e2b_heal_verification_reviewer_fks.py` | `c41a…` | No FIDE change (heals verification reviewer FKs). |
| `d4e5f6a7b8c9_unified_player_registration.py` | `f3d8…` | No FIDE change (registration/payment cols). |
| `e7a2b4c91f05_fide_operation_history_columns.py` | `d4e5…` | Adds `file_name / error_category / records_skipped / duration_seconds` to `fide_imports` (nullable-safe, backfill `0`). |
| `b3f9c2d74a61_fide_import_jobs.py` | `e7a2…` | Creates `fide_import_jobs` only. **Unapplied head.** |

`migrations/env.py` is standard Flask-Migrate (no FIDE logic).
`docs/DEPLOYMENT.md:139-150` is stale (lists `e7a2…` as head).

## 7. Estimated row counts (CURRENT BEHAVIOR — qualitative, not measured)

Code comments cite ~1.9 M rows per full XML file, ~847 MB XML / ~50 MB ZIP.
Runtime is IRI-filtered (`FIDE_ALLOWED_FEDERATIONS = ["IRI"]`,
`config.py:59` → `parser.py:50-86`), so resident `fide_players` rows are the
IRI subset, while `fide_ratings` accumulates `(player × period × type)` only
for imported periods. No purge of DB rows exists; the 90-day retention
governs files only (`storage.py:cleanup_old_files`). RECOMMENDED FUTURE:
record actual `SELECT COUNT(*)` per table and per-period growth during
Phase 1 data-migration rehearsal; size the FIDE Service DB and indexes from
that measurement.

## 8. What moves unchanged vs redesigned (RECOMMENDED FUTURE)

* **Move unchanged:** `fide_players` columns verbatim (including all four
  title columns and `birth_year String(4)` verbatim semantics);
  `fide_ratings` columns + `UQ(fide_id, period, rating_type)` + both
  indexes + FK; `fide_import_jobs` state machine columns verbatim
  (the CLI-worker protocol depends on them).
* **Redesign candidates:** `fide_imports` history model (keep as operation
  log but confirm `status` vocabulary vs job `state` vocabulary — currently
  two overlapping models); add explicit dataset-version/period metadata
  surfaced by the API (`period`, `imported_at`, source URL per type);
  add read indexes for API access patterns (`name` trigram/prefix,
  `(federation)`, `(period, rating_type)` covering index) only after
  measuring query plans; decide `birth_year` normalization (UNKNOWN — see
  `12-open-decisions.md`).
* **Do not move:** `player_profiles`, `tournament_participants`,
  `player_verifications`, `users` — Swiss-owned, stay in Swiss DB. The only
  cross-database link is the `fide_id` string value, never a FK.
