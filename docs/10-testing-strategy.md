# 10 — Testing Strategy

`tests/conftest.py` (CURRENT): function-scoped in-memory SQLite,
`WTF_CSRF_ENABLED=False`, `create_all/drop_all` per test. Almost every
DB-touching FIDE test is therefore **tightly coupled to the Swiss DB
schema** (same `db` object); only pure-parser tests are decoupled.

## 1. Test layers for the FIDE Service (RECOMMENDED FUTURE)

| Layer | Scope | Notes |
|---|---|---|
| Domain tests | `parser` (federation filter incl. case-sensitivity, blank `fideid` skip, bad-int skip, inactive, legacy vs new tag names, streaming `iterparse`), `validation` (`REQUIRED_PLAYER_FIELDS`), `errors` (`classify_exception` taxonomy) | Keep CURRENT `tests/domain/test_fide_domain.py`, `tests/test_fide_parser.py` — they are already decoupled (tmp XML only). Move verbatim. |
| Parser edge tests | Per-type `rating_type=` isolation, three-file identity merge fixtures (players A/B/C) | From `test_fide_three_file.py` (job-service parts stay; Swiss-DB parts split out). |
| Importer tests | `tick()` ordering (`standard→rapid→blitz`), `blocked` gating, `WORKER_STALE` sweep, `retry_job` (failed→pending only), heartbeat writes, idempotent upserts, `BATCH_SIZE` batching, success-deletes-archive / failure-keeps | From `test_fide_three_file.py` + `test_fide_acquisition.py` (downloader/classification, upload + server-file pipelines). Must run against the FIDE-only schema. |
| Repository tests | `search_players` (numeric prefix vs per-word `ILIKE` + federation), `get_latest_rating / get_all_latest_ratings / get_latest_for_fide_ids / get_rating_history`, `claim_next_job` reasons, `fail_stale_jobs` | From `tests/infrastructure/test_repositories.py` (FIDE sections) — rewrite fixtures to FIDE-only `db`. |
| Model/migration tests | Defaults, UQ `(fide_id, period, rating_type)` / `(period, rating_type)`, FK, indexes; upgrade `bb0160… → e7a2… → b3f9…` on a scratch DB | From `test_models.py::TestFideImportModel`, `test_deployment_schema.py`, `test_baseline_checkpoint.py`, `test_deployment_paths.py`. |
| API contract tests | Every endpoint in `05-api-contract.md`: auth (401/403), validation (400), pagination, `include_*` flags, error envelope, `dataset` block, ETag/Cache-Control, batch partial success, admin separation | New. Use Flask test client against FIDE-only app; golden JSON fixtures. |
| Upstream integration tests | `build_rating_url`, `verify_rating_url`, `download_fide_zip` (mock HTTP), `run_diagnostics` ladder, storage ZipSlip/`_ZipMemberStream`/retention | From `test_fide_acquisition.py` + `test_storage_gateway.py::TestFideStorage`. Mark network-touching tests; default to mocks. |
| Resilience tests | Matrix in `08-failure-and-resilience.md`: timeout, 429+`Retry-After`, 5xx, malformed JSON, empty dataset, stale period | New. Fault-injection against the Swiss client (see below). |

## 2. Swiss-side tests after extraction (RECOMMENDED FUTURE)

| Layer | Scope |
|---|---|
| Swiss client tests | DTO mapping, `""`/short-query short-circuits, `404→None`, `401→FideAuthError`, timeout→`FideUnavailableError`, batch chunking, secret redaction, timeout/retry discipline | New, no DB. |
| Swiss consumer tests | C2 auto-fill fallback (`0/20`), C3 empty-history, C4/C5 fail-closed, C1 empty-search, C11 `Unknown` badge — each with stubbed client (ok / miss / unavailable / auth-fail) | Adapted from `test_phase8.py`, `test_payment_flow.py:TestD4FideRatingFallback`, `test_fide_verification.py`, `test_fide_title_protection.py`, `test_unified_registration.py`. |
| Snapshot tests | Pairing/standings/prints/exports never call the client (assert no HTTP in those paths) | New regression guard for the snapshot invariant. |
| Edit-bug regression | `player_edit` round-trip preserves `fide_id`/`k_factor`/full titles (fails today) | New; blocked until the Swiss-side fix lands (see `12-open-decisions.md`). |

## 3. Existing tests tightly coupled to the Swiss DB (CURRENT — inventory)

Must be split, not moved blindly: `test_fide_acquisition.py` (881 lines),
`test_fide_three_file.py` (456 lines), `test_fide_import_flow.py`
(incl. known drift `test_status_endpoint_authz_matrix` — expects 302, gets
403), `test_fide_verification.py`, `test_fide_title_protection.py`,
`application/test_admin_auth_fide.py::TestFide`,
`application/test_registration_payment.py::test_fide_title_not_taken_from_form`,
`infrastructure/test_models.py::TestFideImportModel`,
`infrastructure/test_repositories.py`,
`infrastructure/test_storage_gateway.py::TestFideStorage`,
`integration/test_fide_notification_backup_integration.py`,
`web/test_web_player_fide_print_backup.py`,
`test_beta_profile.py::TestBetaVerification`,
`test_phase8.py::TestPhase8`, `test_payment_flow.py::TestD4FideRatingFallback`,
`test_unified_registration.py`, `test_query_performance.py`,
`test_production_hardening.py`, deployment/schema tests above.
Decoupled (move verbatim): `domain/test_fide_domain.py`, `test_fide_parser.py`.

Known pre-existing failures to carry over as context (not caused by this
phase): `test_status_endpoint_authz_matrix` (302 vs 403),
`test_data_integrity::TestC1BackupSwissState` (3 tests),
`test_payment_flow.py` (7 errors) + receipt-lifecycle test
(`PaymentService.gateway` facade drift). See `AGENTS.md` testing
conventions in the Swiss repo.
