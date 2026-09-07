# 01 — Current State: FIDE Inside Swiss Manager

Source tree analyzed: `G:\Programming\Web\swiss_app_dev` (branch `dev`,
HEAD `8501f95 Implement FIDE import job architecture` at time of analysis).
All paths below are relative to that repo unless stated otherwise.
Conventions: `CURRENT BEHAVIOR` = verified in code; `INTENDED BEHAVIOR` =
approved domain decision; `RECOMMENDED FUTURE` = proposal for FIDE Service.

## 1. Layer map (Clean Architecture / DDD)

```text
interfaces/web/fide_routes.py (fide_bp, no url_prefix, /admin/fide/*)
  → application/fide/* (orchestrator, job service, search service)
  → domain/fide/* (parser, validation, errors, models)
  → infrastructure/fide/* (storage, downloader, diagnostics)
  → infrastructure/models/fide.py + infrastructure/repositories/fide.py
```

Dependency rules (per `AGENTS.md`): `domain/` is pure Python (no Flask /
SQLAlchemy / infrastructure / web imports); `application/` owns transactions
(`commit`/`rollback`) and must not import `interfaces/web` or
`flask.request`; repositories are flush-only; blueprints are HTTP-only.

## 2. Domain layer — `domain/fide/`

| File | CURRENT BEHAVIOR |
|---|---|
| `domain/fide/__init__.py` | `fide_profile_url(fide_id) -> str \| None` → `https://ratings.fide.com/profile/{id}`. Injected as Jinja global in `app/__init__.py:206`. Display-only helper. |
| `domain/fide/models.py` | `@dataclass FidePlayerData`: `fide_id, name, federation, sex, title, wtitle, otitle, foatitle, rating_standard/games_standard/k_standard, rating_rapid/games_rapid/k_rapid, rating_blitz/games_blitz/k_blitz, birth_year, inactive`. The canonical in-memory shape of one parsed XML player. |
| `domain/fide/parser.py` | `parse_fide_xml(source, allowed_federations, rating_type)` — streaming `xml.etree.iterparse` generator. Federation filter (`country not in allowed → skip`), `rating_type in (standard, rapid, blitz)`, handles both `<w_title>/<o_title>/<foa_title>/<birthday>` and legacy `<wtitle>/<otitle>/<foatitle>/<birth_year>`. Skips blank `fideid`, bad-int ratings, inactive handling. Exposes `RATING_TYPES`, `_iter_player_elements`, `_first_text`. |
| `domain/fide/validation.py` | `validate_fide_xml(path)`, `validate_fide_stream(factory)` + `REQUIRED_PLAYER_FIELDS = (fideid, name, country)` — structural pre-DB check before any write. |
| `domain/fide/errors.py` | `FideError`, `FideErrorCategory` (16 categories: `NETWORK_*`, `HTTP_*`, `INVALID_*`, `PARSER_ERROR`, …), `STAGE_LABELS`, `CATEGORY_MESSAGES`, `classify_exception()`. Used by downloader, orchestrator, job service, and import-status UI. |
| Non-FIDE-adjacent | `domain/registration/__init__.py` (eligibility gate `fide_verification_required` / `fide_verified`), `domain/pricing/{models,engine}.py` (`PlayerPricingData.fide_title` → title discount), `domain/rating/*` (generic Elo `k_factor` math, **not** FIDE-specific), `domain/pairing/__init__.py` (`__fide_reference__` compliance tag only). |

## 3. Application services — `application/fide/` + facades

| File | CURRENT BEHAVIOR |
|---|---|
| `application/fide/__init__.py` | Package hub re-exporting `FideImportOrchestrator, FideSearchService, FideImportJobService`. New code imports from the package. |
| `application/fide/fide_import_orchestrator.py` | **Synchronous pipeline** (legacy/new hybrid). `execute_import / execute_upload_import / execute_server_file_import / run_diagnostics / latest_status / _save_rating`. Upserts `FidePlayerModel` + up to 3× `FideRatingModel` per player, `BATCH_SIZE = 500`, one `fide_imports` row per period, `STALE_RUN_MINUTES = 15`. Old threaded `execute_import/start_async` removed. Still serves upload + server-file routes and most import tests. |
| `application/fide/fide_job_service.py` | **Job-based pipeline** (current direction). `ensure_period_jobs / get_period_status / retry_job / tick / _upsert_player / _heartbeat / _fail_job`. Persistent 3× `(period, rating_type)` jobs, strict order `standard → rapid → blitz`, `BATCH_SIZE = 500`, idempotent on `fide_id` / `(fide_id, period, rating_type)`. Designed for `flask fide-import-step` Cron. |
| `application/fide/fide_search_service.py` | `FideSearchService.search(query, federation, limit)` → `FidePlayerRepository.search_players` + bulk `get_latest_for_fide_ids`. Returns `{fide_id, name, federation, title, birth_year, sex, standard, rapid, blitz, period}` (0/`""` defaults on miss). |
| `application/fide_import_service.py` | **Legacy facade** delegating 1:1 to `FideImportOrchestrator`. Compatibility shim only. |
| `application/fide_search_service.py` | **Legacy facade** delegating `search()` to `application.fide.fide_search_service`. Compatibility shim only. |
| Verification | `application/verification/{verification_request_service,verification_approver,verification_status_updater}.py` + facade `application/verification_service.py`. Request requires local `FidePlayer` row to exist (`ValueError` in Persian otherwise); blocks duplicate pending/verified claims; per-aspect `verify_fide_id / verify_dob / verify_photo` (True/False/None triad); all-True → `approved + verified` + title sync; any-False → `rejected`. Legacy `VerificationStatusUpdater.approve/reject_request` kept for overall approve path. |
| Swiss consumers | `application/player/participant_management.py` (create/update, FIDE auto-fetch + snapshots — see §11), `application/player/csv_import_service.py` (`fide_id/fide_title/k_factor` CSV columns, `VALID_TITLES`, default `k=20`), `application/registration/*` (eligibility + pricing via `profile.fide_title`, never form title — `P0-F` rule), `application/admin/{dashboard_stats_service,system_health_service}.py` (`fide` stats card + `Available/Empty` health via `FidePlayerModel.query.count()` and `FIDE_DATA_DIR` disk usage), `application/player_profile_service.py` (profile + rating-history reads). |

**Legacy vs preferred (CURRENT BEHAVIOR):** orchestrator + both facades = legacy/compat path (still live for uploads). Job service + `FideImportJobModel` + `flask fide-import-step` CLI + `trigger_import/retry_job` routes = preferred path. Both write the same `fide_players` / `fide_ratings` tables; the job path writes only its own `rating_type`, the orchestrator writes all three.

## 4. Repositories — `infrastructure/repositories/fide.py`

* `FidePlayerRepository`: `get_by_fide_id / get_by_fide_ids / save / search_players` (numeric → `fide_id LIKE query%`, else per-word `name ILIKE %term%` + optional federation filter) / `get_latest_rating / get_all_latest_ratings`.
* `FideRatingRepository`: `get / save / get_latest_for_fide_ids / get_rating_history`.
* `FideImportJobRepository`: `get / get_for_period / save / fail_stale_jobs (WORKER_STALE) / claim_next_job` (returns `claimed / busy / stale_present / blocked / failed_present / nothing_pending`, `WITH FOR UPDATE`, enforces `standard → rapid → blitz` gating).
* `FideImportRepository`: `get_by_period / save / get_all`.
* Constants: `RATING_TYPE_ORDER = (standard, rapid, blitz)`, `ACTIVE_JOB_STATES`.
* Adjacent: `infrastructure/repositories/profile.py:get_by_fide_id` (local `player_profiles`, not `fide_players`), `infrastructure/repositories/verification.py` (verification queue). All flush-only per architecture rules.

## 5. Infrastructure — ORM, storage, downloader, diagnostics

* `infrastructure/models/fide.py` — 4 models, fully documented in `docs/04-database.md`: `FidePlayerModel` (`fide_players`, `fide_id` PK `String(20)`), `FideRatingModel` (`fide_ratings`, FK → `fide_players`, UQ `(fide_id, period, rating_type)`), `FideImportJobModel` (`fide_import_jobs`, UQ `(period, rating_type)`), `FideImportModel` (`fide_imports`, one row per period). No ORM `relationship()` in this file; joins are by key in repositories.
* Swiss-side mirrors (no FK to FIDE): `infrastructure/models/profile.py` (`PlayerProfileModel.fide_id` nullable indexed, `fide_title`, `fide_verification_status`), `infrastructure/models/participant.py` (`TournamentParticipantModel.fide_title_snapshot`, `k_factor`, `rating_snapshot`), `infrastructure/models/verification.py` (`PlayerVerificationModel.requested_fide_id` non-null + reviewer FKs to `users.id`).
* `infrastructure/fide/storage.py` — `FideStorageManager`, `get_period_string() → YYYY-MM`, `tmp_zip_path(base, period, type) → tmp/<period>/<type>.zip`, ZipSlip-safe `open_zip_member / _ZipMemberStream / inspect_zip`, `count_players_in_xml/stream`, `save_uploaded_file / prepare_uploaded_source / cleanup_old_files`. Never extracts the ~847 MB XML; streams the ZIP member via `iterparse`. Retention 90 days.
* `infrastructure/fide/downloader.py` — `build_rating_url(base, type, period) → {type}_{mon}{yy}frl_xml.zip`, `verify_rating_url` (HEAD), `download_fide_zip(url, dest, progress_cb, connect/read timeout, retries, backoff)` with `FideError` taxonomy + 429/permanent-status handling, `.part` + atomic `os.replace`.
* `infrastructure/fide/diagnostics.py` — `run_diagnostics(url) → {url_host, steps[5× dns/connect/tls/http/content], reachable}` connectivity ladder for the admin UI.
* `infrastructure/providers/coronate_{parser,generator}.py` — maps `fideId/fide_id ↔ profile.fide_id` in backups.

## 6. Routes — `interfaces/web/fide_routes.py` (`fide_bp`)

All `login_required + admin_required` (system admin only, 403 otherwise):

| Method + path | Handler | CURRENT BEHAVIOR |
|---|---|---|
| `GET /admin/fide` | `fide_dashboard` | Import dashboard (trigger/diagnostics/upload/server-file forms + jobs table). Template `templates/admin/fide_dashboard.html`, polls `import_status`. |
| `POST /admin/fide/import` | `trigger_import` | `ensure_period_jobs(current_period)` then returns immediately — no threads. |
| `POST /admin/fide/jobs/retry/<rating_type>` | `retry_job` | `failed → pending` only; completed/active raise `ValueError`. |
| `POST /admin/fide/upload` | `upload_import` | Synchronous staged-upload import (`fide_file` field). |
| `POST /admin/fide/import/server-file` | `import_server_file` | Synchronous import of a server-side file. |
| `POST /admin/fide/diagnostics` + `GET /admin/fide/diagnostics.json` | `run_diagnostics / diagnostics_json` | Connectivity ladder. |
| `GET /admin/fide/import/status` | `import_status` | `fide_imports` row + `jobs[]` for the period (dashboard polling). |
| `GET /admin/fide/search?q=&federation=IRI` | `search` | JSON via `FideSearchService` (default federation `IRI`). |
| `GET /admin/fide/verifications` + `POST .../<req_id>/{approve,reject,verify_fide_id,verify_dob,verify_photo}` | verification handlers | Approval queue delegating to verification services. |

Other blueprints touching FIDE: `dashboard_routes.py` (`GET /dashboard/api/fide-search`, `POST /dashboard/profile/update` deliberately ignores `fide_title` — `P0-F`; verification request reuses `profile.fide_id`), `player_routes.py` (`/<public_id>/players/add|/<id>/edit|/import`, CSV template header includes `fide_id, fide_title, k_factor`), `player_profile_routes.py` (`GET /player/<identifier>` — `get_by_fide_id` if non-digit, 403 if `fide_verification_status != verified`), `registration_routes.py` (`POST /<public_id>/register` strips `fide_id`, prices via `profile.fide_title` only), `print_routes.py` (rating list uses `fide_title_snapshot + profile.fide_id`). `app/__init__.py:107,206` registers `fide_bp`, anchors `FIDE_DATA_DIR = <instance>/data/fide`, derives `MAX_CONTENT_LENGTH = max(8MB, FIDE_UPLOAD_MAX_BYTES)`, injects `fide_profile_url` global.

## 7. Templates referencing FIDE

Admin: `templates/admin/{fide_dashboard.html, verifications.html, base.html, dashboard.html, system.html, user_detail.html}`. Dashboard: `templates/dashboard/{verification_request.html, index.html, create_profile.html}` (search box → `/dashboard/api/fide-search`, readonly `fide_id`, `P0-F` disabled `fide_title`). Tournament: `templates/tournament/{players.html, player_add.html (FIDE Search Card, debounced 300 ms, ≥3 chars), player_detail.html, player_import.html, pricing.html, register.html, registrations.html, summary.html, view.html, crosstable.html}`, `templates/print/{round.html, standings.html}` (snapshot titles), `templates/player/public_profile.html`.

## 8. CLI — `app/cli.py`

* `flask fide-import-step [--period YYYY-MM] [--timeout SECS]` — claims **one** eligible `fide_import_jobs` step via `FideImportJobService.tick()`, prints `OK / IDLE / ERROR`, exits. Designed for cPanel Cron every few minutes; concurrent runs safely report `busy / blocked`. No auth (server-shell trust).
* `flask create-admin / init-db` — non-FIDE (`init-db` creates the FIDE tables too).
* `scripts/` contains only `__init__.py` — **zero** FIDE scripts. `run.py` has **zero** FIDE references (just `create_app()`).

## 9. Migrations touching FIDE — `migrations/versions/`

| File | CURRENT BEHAVIOR |
|---|---|
| `bb0160eefd6b_fresh_baseline_schema_clean_production_.py` (`down_revision=None`) | Squashed baseline creating `fide_imports`, `fide_players` (+ federation index), `fide_ratings` (FK + UQ + indexes), plus `player_profiles.fide_id/fide_title/fide_verification_status`, `player_verifications`, `tournament_participants.fide_title_snapshot/k_factor`. |
| `e7a2b4c91f05_fide_operation_history_columns.py` | `ALTER fide_imports ADD file_name / error_category / records_skipped (backfill 0) / duration_seconds` (nullable-safe). |
| `b3f9c2d74a61_fide_import_jobs.py` | `CREATE fide_import_jobs` (+ UQ + indexes, table-exists guard). **Header warns it HAS NOT BEEN APPLIED anywhere — do not run `flask db upgrade` without explicit approval.** Current head; `docs/DEPLOYMENT.md` is stale (lists `e7a2…` as head). |
| `f3d8a1c47e2b_heal_verification_reviewer_fks.py` | Heals `player_verifications` reviewer FKs to `users.id`. No FIDE-table change. |
| `c41a9e2b07d3_*`, `d4e5f6a7b8c9_*` | No FIDE-table change. |

## 10. Configuration, filesystem, external dependencies

From `config.py` (79 lines, verified): `FIDE_ALLOWED_FEDERATIONS = ["IRI"]` (hardcoded filter → `parse_fide_xml`), `FIDE_XML_URL = http://ratings.fide.com/download/players_list_xml.zip` (legacy combined list), `FIDE_DOWNLOAD_BASE_URL` (env-overrideable, default `http://ratings.fide.com/download/`, feeds `build_rating_url`), `FIDE_DATA_DIR` (resolved in `app/__init__.py` to `<instance>/data/fide`, layout `<YYYY-MM>/players_list_xml.zip|.xml` + `tmp/<YYYY-MM>/<type>.zip` + staged uploads), `FIDE_RAW_RETENTION_DAYS = 90`, `FIDE_UPLOAD_MAX_BYTES = 120 MB` (env), `FIDE_CONNECT_TIMEOUT = 10` / `FIDE_READ_TIMEOUT = 30` / `FIDE_MAX_RETRIES = 3` (env), `FIDE_WORKER_TIMEOUT_SECONDS = 600` (env, heartbeat staleness → `WORKER_STALE`).

`.env` (local) contains only messenger/payment keys (`BALE_*`, `TELEGRAM_*`, `ZARINPAL_*`); no `DB_*`, `SECRET_KEY`, or `FIDE_*` locally — names only were inventoried, values never read. No in-repo crontab; Cron assumption lives in code/docstrings (`fide-import-step` every few minutes; no threads/Celery/Redis). External dependency: `http://ratings.fide.com/download/` (plain HTTP base URL in config — note for `docs/09-deployment.md` / `docs/12-open-decisions.md`). `requirements.txt`: Flask 3.1.3, Flask-SQLAlchemy 3.1.1, Flask-Migrate 4.1.0, SQLAlchemy 2.0.51, PyMySQL 1.1.1, requests 2.32.4; prod Python 3.12 per docs (dev observed 3.14.5).

`data/fide/` and `instance/data/` are empty in the repo; `data/`, `instance/`, `*.db`, `*.log`, `*.zip` are gitignored. Scale comments cite ~847 MB XML / ~50 MB ZIP / ~1.9 M rows, `BATCH_SIZE = 500`, commit-per-batch.

## 11. Title data (CURRENT BEHAVIOR — do not reinterpret)

* Defined: `domain/fide/models.py:FidePlayerData.title/wtitle/otitle/foatitle` ← parsed from `<title> / <w_title|wtitle> / <o_title|otitle> / <foa_title|foatitle>`; stored in `infrastructure/models/fide.py:FidePlayerModel.title/wtitle/otitle/foatitle String(10) default ""` (overwritten on every import in both pipelines: orchestrator `_save_rating` path, job service `_upsert_player`).
* Local owned copies (never written by import directly): `PlayerProfileModel.fide_title String(5)`, `TournamentParticipantModel.fide_title_snapshot String(5)`. Set only on verification approval or arbiter edit/CSV.
* Sync rule (CURRENT): `verification_status_updater.py:52-57`, `verification_approver.py:95-102` → `official = (fide.title or wtitle or otitle or foatitle).strip(); if official: profile.fide_title = official`. Priority is positional `title → wtitle → otitle → foatitle`, first non-empty wins, empty official → keep existing.
* Copy rule (CURRENT): `participant_management.py` create/update copies form `fide_title → profile.fide_title → participant.fide_title_snapshot`.
* Protection rule (CURRENT, `P0-F`): player-supplied `fide_title` is ignored in `dashboard_routes.py:292`, `registration_routes.py:208`, `auth/profile_creation_service.py:54`, `templates/dashboard/index.html:418`; pricing uses `profile.fide_title` only (covered by `tests/test_fide_title_protection.py`).
* `RECOMMENDED FUTURE`: preserve the four-column representation verbatim in the FIDE Service DB; expose a single documented `display_title` derivation only at the API boundary if needed — do not collapse columns or change the priority without a new business decision.

## 12. Known Swiss-side issue — `player_edit.html` (CURRENT BEHAVIOR, NOT fixed in Phase 0)

* Template `templates/tournament/player_edit.html:40-51` renders only `rating` + `fide_title ∈ {"", GM, IM, FM}` (truncated list — no WGM/WIM/etc.), and **no `fide_id` or `k_factor` inputs**.
* Handler `interfaces/web/player_routes.py:player_edit:82-95 → ParticipantManagement.update` unconditionally executes (verified in `application/player/participant_management.py:131-159`): `profile.fide_id = form["fide_id"].strip()` (missing key → `""` = **link wiped**), `profile.fide_title = form["fide_title"].strip()` (missing/other → wiped/downgraded, no official-title guard unlike verification), `participant.k_factor = int(form.get("k_factor", 20) or 20)` (missing → **reset to 20**), `rating_snapshot = int(form.get("rating", 0) or 0)` (no auto-fetch unlike `create`), `fide_title_snapshot = profile.fide_title`.
* `INTENDED BEHAVIOR` (per domain decisions): Swiss owns participant state; an edit form must not silently destroy `fide_id`/title/`k_factor`.
* `RECOMMENDED FUTURE`: fix on the Swiss side during extraction (add `fide_id + k_factor` inputs or hidden preserves + full title list, or make `update()` ignore missing keys), and carry the invariant into the client contract (`docs/06-swiss-client-contract.md`: explicit `resync` semantics, no silent wipe). Tracked as an open item in `docs/12-open-decisions.md`.
