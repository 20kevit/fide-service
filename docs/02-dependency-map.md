# 02 — Dependency Map: Every Swiss → FIDE Consumer

All paths relative to `swiss_app_dev`. Every consumer below is **Swiss-side,
local-DB only** (`fide_players` / `fide_ratings` populated by monthly XML).
No request path queries `ratings.fide.com` live except the import worker
(`infrastructure/fide/downloader.py`). All consumers are synchronous except
the import worker (Cron-driven CLI).

## Consumer table

| # | Source file : class/function | Caller / route | Operation | Data requested | Sync/async | Failure behavior (CURRENT) | Migration requirement (RECOMMENDED FUTURE) |
|---|---|---|---|---|---|---|---|
| C1 | `application/fide/fide_search_service.py:FideSearchService.search` | `fide_routes.py:search (GET /admin/fide/search)`, `dashboard_routes.py:dashboard_fide_search (GET /dashboard/api/fide-search)`; JS in `player_add.html:151`, `verification_request.html:141` | Player search | `FidePlayerRepository.search_players` + bulk `get_latest_for_fide_ids` → `{fide_id, name, federation, title, birth_year, sex, standard, rapid, blitz, period}` | Sync | Empty query → `[]`; dashboard `<3 chars` → `[]`; missing ratings → `0`; no exception path | `GET /api/v1/players/search` |
| C2 | `infrastructure/repositories/fide.py:FidePlayerRepository.get_latest_rating / get_all_latest_ratings` via `application/player/participant_management.py:ParticipantManagement.create:82-90` | `player_routes.py:player_add`, `registration_approver.py:approve_registration:74-97` (builds `form_data` with `rating="0"` then `create`) | Rating + K auto-fill on participant create | Latest `(rating, games, k_factor)` for `(fide_id, rating_type)` where `rating_type = tournament.time_control_type ∈ {standard, rapid, blitz} else standard`, only when `rating == 0 and profile.fide_id` | Sync | Miss → keep `0 / 20` silently (verified by `test_payment_flow.py:TestD4FideRatingFallback`, `test_phase8.py` rapid-1900 case); no verification-status check | `GET /api/v1/players/{id}/ratings` (latest) with local fallback to `0/20` |
| C3 | `application/player_profile_service.py:get_profile_data / get_rating_history` | `player_profile_routes.py:public_profile (GET /player/<identifier>)` | Public profile + rating-history chart | `get_by_fide_id` + `get_all_latest_ratings` → `{standard, rapid, blitz: {rating, games, k_factor, period}}`; `get_rating_history` grouped asc | Sync | No `fide_id` → `{"standard":[],"rapid":[],"blitz":[]}`; missing player → ratings `None`; non-`verified` `fide_id` URL → 403/404 | `GET /api/v1/players/{id}` + `GET /api/v1/players/{id}/ratings` |
| C4 | `application/verification/verification_request_service.py:submit_request` | `dashboard_routes.py:request_verification (GET/POST /dashboard/verification/request)` | Claim a FIDE ID | `FidePlayerRepository.get_by_fide_id(requested_fide_id)` existence check | Sync | **Fail closed:** miss → `ValueError(کد فیده در پایگاه داده محلی یافت نشد...)`; duplicate pending/verified → blocked | `GET /api/v1/players/{id}` must exist before accepting claim (fail closed preserved) |
| C5 | `application/verification/{verification_approver,verification_status_updater}.py` | `fide_routes.py` verification POST handlers | Title sync on approval | In-memory `FidePlayer` row already loaded: `official = title \|\| wtitle \|\| otitle \|\| foatitle` → `profile.fide_title` | Sync | Empty official → keep existing; any-False aspect → `rejected` | `GET /api/v1/players/{id}` for official titles at approval time |
| C6 | `application/registration/registration_creator.py:create_registration:170-172` | `registration_routes.py:POST /<public_id>/register` | Profile dedup on registration | `PlayerProfileRepository.get_by_fide_id` (local `player_profiles`, **not** `fide_players`) | Sync | Miss → create new `PlayerProfileModel(fide_id=raw, fide_title="")`; IRI default federation for new profiles | No FIDE call; keep local. Optional `GET /api/v1/players/{id}` prefill must not block registration |
| C7 | `application/registration/{eligibility_checker,pricing_calculator}.py` + `registration_approver.py` | Registration flow | Eligibility + pricing | `profile.fide_verification_status == "verified"`; `profile.fide_title` (already synced, never live lookup) | Sync | Unverified → eligibility failure message; pricing ignores posted `fide_title` (`P0-F`) | No FIDE call in hot path; snapshot only |
| C8 | `application/player/participant_management.py:update:131-159` | `player_routes.py:player_edit (GET/POST /<public_id>/players/<id>/edit)` | Participant edit | **No FIDE read** — but unconditionally writes `profile.fide_id / profile.fide_title / participant.k_factor / fide_title_snapshot` from form | Sync | Missing keys → wipe `fide_id`/`fide_title`, reset `k_factor → 20` (known issue, see `01-current-state.md` §12) | `PUT /api/.../participants/{pid}` semantics must forbid silent wipe; explicit `resync` op |
| C9 | `application/player/csv_import_service.py:85-102` | `player_routes.py:player_import` | CSV bulk import | Takes `fide_id / fide_title / k_factor` columns literally (`VALID_TITLES`, default `k=20`); no lookup | Sync | Invalid → row error; no auto-fetch | Optional batch validate via `POST /api/v1/players/batch` (non-blocking) |
| C10 | `application/import_export/{import_service,export_service,custom_json_provider}.py` | Backup/restore routes | Backup identity mapping | `profile.fide_id` match for white/black lookup; `fide_id/fide_title` in backup JSON | Sync | Miss → new profile / `0` rating path | Keep local; no FIDE call |
| C11 | `application/admin/dashboard_stats_service.py:43-47`, `system_health_service.py:25,32-40` | Admin dashboard / system pages | Stats + health | `FidePlayerModel.query.count()`, `FideImportModel` last row, `FIDE_DATA_DIR` disk usage | Sync | Empty → `Available/Empty` badge, `0` counts | `GET /api/v1/status` (+ cache headers) for health |
| C12 | `interfaces/web/print_routes.py:170-186`, standings services | Print / standings | Display | `participant.fide_title_snapshot + profile.fide_id` (snapshots, never live) | Sync | Always renders from snapshot | No FIDE call; snapshot preserved |
| C13 | Import worker: `application/fide/{fide_import_orchestrator,fide_job_service}.py` + `app/cli.py:fide-import-step` + `infrastructure/fide/{storage,downloader,diagnostics}.py` | `POST /admin/fide/import|/upload|/import/server-file`, Cron `flask fide-import-step`, `GET /admin/fide/import/status` | Download → validate → stream-parse → batch upsert → cleanup | `ratings.fide.com/download/` archives; local `FIDE_DATA_DIR` staging | Async via DB jobs (HTTP returns immediately; CLI claims one step) | `FideError` taxonomy, `WORKER_STALE`, explicit retry only, success deletes archive, failure keeps for retry | Moves entirely into FIDE Service (see `07-import-architecture.md`) |

## Dependency graph (text)

```text
[player_add.html JS] ──GET /dashboard/api/fide-search──▶ [dashboard_routes]
[verification_request.html JS] ──GET /dashboard/api/fide-search──▶ [dashboard_routes]
[admin fide_dashboard.html] ──GET /admin/fide/search──▶ [fide_routes]
        │                        POST /admin/fide/import|/upload|/server-file|/diagnostics
        │                        GET  /admin/fide/import/status
        ▼
[FideSearchService] ──▶ [FidePlayerRepository + FideRatingRepository]
[ParticipantManagement.create] ──▶ [FidePlayerRepository.get_latest_rating]
[PlayerProfileService] ──▶ [FidePlayerRepository + FideRatingRepository]
[VerificationRequestService/Approver/StatusUpdater] ──▶ [FidePlayerRepository]
[RegistrationCreator/Approver] ──▶ [PlayerProfileRepository.get_by_fide_id] (local)
                                └─▶ [ParticipantManagement.create] ──▶ [FidePlayerRepository]
[DashboardStatsService/SystemHealthService] ──▶ [FidePlayerModel/FideImportModel + FIDE_DATA_DIR]
[FideImportOrchestrator / FideImportJobService + CLI fide-import-step]
        ──▶ [storage + downloader + diagnostics + parser + validation]
        ──▶ [FidePlayerModel/FideRatingModel/FideImportModel/FideImportJobModel]
```

## Import-level evidence (grep `from.*fide|import.*fide`)

`app/__init__.py:107,206` (`fide_bp`, `fide_profile_url`); `app/cli.py:41-42`
(storage + job service); `application/fide/__init__.py:6-8`;
`application/fide/fide_search_service.py:8`, `fide_job_service.py:39-52`,
`fide_import_orchestrator.py:64-76,309,382`;
facades `application/fide_import_service.py`, `application/fide_search_service.py`;
`application/player/participant_management.py:14`;
`application/player_profile_service.py:4`;
`application/verification/*:14`; `application/admin/dashboard_stats_service.py:13`,
`system_health_service.py:12`; `interfaces/web/fide_routes.py:7-16`,
`dashboard_routes.py:13`; `infrastructure/repositories/fide.py:10`,
`models/__init__.py:39`, `repositories/__init__.py:29`.

Files matching `**/*fide*`: `interfaces/web/fide_routes.py`,
`application/fide/{fide_search_service,fide_job_service,fide_import_orchestrator}.py`,
`infrastructure/{models,repositories}/fide.py`,
`infrastructure/fide/{storage,downloader,diagnostics}.py`,
`domain/fide/{parser,validation,errors,models}.py`,
`templates/admin/fide_dashboard.html`,
`migrations/versions/{b3f9c2d74a61_fide_import_jobs,e7a2b4c91f05_fide_operation_history_columns}.py`.

## What must the REST API cover (derived, not invented)

At minimum: player search, get player, get latest ratings, get rating
history, batch lookup, and a status/health read — because C1–C5, C11 each
need one. Import/admin operations (C13) move inside the service and are
exposed only as separated, protected admin endpoints. Full contract in
`docs/05-api-contract.md`; Swiss-side interface in
`docs/06-swiss-client-contract.md`.
