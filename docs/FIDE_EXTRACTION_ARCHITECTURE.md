# FIDE Extraction Architecture (Executive Reference)

Primary architectural reference for all future coding agents. Details live
in `docs/01–12`; this document summarizes and points, never duplicates
authority. Evidence cited as `swiss_app_dev:path[:line]`.
`CURRENT BEHAVIOR` = verified in code; `INTENDED BEHAVIOR` = approved
decision; `RECOMMENDED FUTURE` = explicit proposal.

## 1. Current architecture

Flask + Clean Architecture/DDD (`run.py → app/create_app()`,
`interfaces/web → application → domain + infrastructure`). FIDE is an
in-process subsystem: `interfaces/web/fide_routes.py (fide_bp)` →
`application/fide/{fide_import_orchestrator, fide_job_service,
fide_search_service}` (+ legacy facades) → `domain/fide/{parser,
validation, errors, models}` → `infrastructure/fide/{storage, downloader,
diagnostics}` → `infrastructure/models/fide.py +
infrastructure/repositories/fide.py`. All consumers are Swiss-side and
local-DB only (monthly XML into `fide_players`/`fide_ratings`); no live
upstream read in request paths. Two pipelines coexist: synchronous
orchestrator (upload/server-file, one `fide_imports` row/period) and
preferred job-based service (3× `(period, rating_type)` jobs,
`flask fide-import-step` Cron, strict `standard→rapid→blitz` order,
heartbeats, `WORKER_STALE`, explicit retry). Full map: `docs/01`.

## 2. Target architecture

```text
Swiss Manager (swiss_app_dev, own repo/venv/DB/config/migrations)
      │  HTTPS REST API v1, X-API-Key, fail closed (docs/05)
      ▼
FIDE Service (fide_service, own repo — THIS repo — own Flask app,
  own Clean layers, own DB/venv/config/migrations/storage/workers/
  Cron/deployment/tests)
```

Swiss depends only on the client contract (`docs/06`); it never touches
FIDE ORM/repos/DB/filesystem again. Import ownership moves entirely.
Structure proposal in `docs/09` §1 (derived from current layers, not
invented).

## 3. Bounded context

FIDE context: current identity + current ratings + rating history + import
processing/jobs + source-data lifecycle. Swiss context: local players,
participants, registration, tournament state, snapshots, tournament truth.
Verification workflow state stays Swiss; it reads FIDE through the API.
Title-discount pricing reads the Swiss-owned `profile.fide_title` copy.
Dependency graph + per-consumer table: `docs/02`.

## 4. Data ownership

Table: `docs/03` §2. Authoritative (FIDE): identity fields, four title
columns, per-`(period, type)` `(rating, games, k_factor)`, dataset version.
Snapshot (Swiss, frozen): `rating_snapshot / k_factor /
fide_title_snapshot`. Synchronized copies: `profile.fide_title`
(on approval), `profile.fide_id` (on claim/create/edit/CSV).
Ambiguous items (DB-level `fide_id` non-unique vs app enforcement,
`birth_year` representation, empty-title re-approval, reactivation
no-refetch) are tracked in `docs/12` — confirm, don't assume.

## 5. FIDE identity model

FIDE side: `fide_id` NOT NULL + UNIQUE + PK/business key
(`infrastructure/models/fide.py:13`). Swiss side: `fide_id` NULLABLE
(`models/profile.py:14`), absent for non-FIDE players, uniqueness enforced
in app for pending/verified claims. Parser requires `fideid`
(`validation.py`), skips blanks. Verification requires the FIDE row to
exist (fail closed). Client rule: no silent wipe — missing keys never mean
`""`/`20` (response to the `player_edit.html` bug, `docs/01` §12).

## 6. REST API boundary

v1 (`docs/05`): `GET /players/{id}`, `GET /players/search`,
`GET /players/{id}/ratings`, `GET /players/{id}/ratings/history`,
`POST /players/batch`, `GET /status` (+ `GET /healthz`), plus separated
protected admin (`POST/GET /admin/imports`, `GET /admin/jobs`,
`POST /admin/jobs/retry`, `GET /admin/diagnostics`). Cross-cutting:
HTTPS, API key + `hmac.compare_digest`, stable error envelope with
`request_id`, pagination (`limit` default 20/max 100), timeouts (server
≤ 5 s; client 3 s/8 s + one GET retry), ETag/`Cache-Control`, and a
`dataset{period, imported_at, source}` block on every data response.
Covers exactly C1–C5 + C11; no overengineering (no live proxy, no writes
from Swiss, no webhooks, no GraphQL).

## 7. Swiss/FIDE responsibility split

FIDE serves reads + runs imports; Swiss owns snapshots and all tournament
logic. Per-consumer contract (`docs/06` §3): search → `[]`+hint;
create auto-fill → keep `0/20`; profile/history → local render + empty
chart; verification request/approval → fail closed; registration dedup/
eligibility/pricing → local-only (optional non-blocking prefill); CSV →
warn-and-continue; stats/health → `Unknown`/`Empty` badges. Client layout
`application/fide_client/{client,dto,errors,cache,fallback}.py`, secret
hygiene, timeout discipline, snapshot-first rule.

## 8. Import ownership

Sole pipeline = job-based (`docs/07` §4): streaming `iterparse` (never
extract ~847 MB XML), ZipSlip-safe access, atomic `.part` replace,
`BATCH_SIZE=500` commit-per-batch, idempotent upserts, heartbeat protocol,
`WORKER_STALE` sweep, explicit-retry-only, success-deletes-archive /
failure-keeps, 90-day file retention, per-type archives
`{type}_{mon}{yy}frl_xml.zip`. Moved verbatim: domain + storage/downloader/
diagnostics + job state machine + schemas + worker semantics. Swiss keeps
no import code, no `FIDE_DATA_DIR`, no FIDE Cron. `fide_imports` becomes
rollup/history; jobs are execution truth (vocabulary confirmed Phase 1).

## 9. Failure model

Matrix + per-consumer behavior: `docs/08`. `404` = normal miss (not error);
`401/403` = fail closed + alert; `429` honors `Retry-After` then degrades;
`5xx`/malformed/`503` = unavailable → snapshots/cache/empty per consumer;
stale period flagged, verification requires freshness (threshold N
confirmed Phase 1). Malformed `200` ≡ unavailable, never partially applied.
Logging: `request_id`, endpoint, `fide_id`, latency, outcome — no keys,
bodies, or extra PII.

## 10. Security model

HTTPS; API key in `X-API-Key` (RECOMMENDED, confirm Phase 1), env-only,
compared with `hmac.compare_digest`, fail closed, never logged; separate
admin scope/key + audit log; rate limiting (baseline now, tune Phase 6);
no session cookies or per-user auth on reads; `401/403/429/5xx` without
leaks. Current codebase has zero API-key infra (session + admin gates +
webhook secrets only) — this is new surface,最小 by design.

## 11. Database strategy

Move `fide_players` + `fide_ratings` (+UQ/FK/indexes) and
`fide_import_jobs` state machine verbatim; `fide_imports` redesigned as
rollup/history (`docs/04` §8). Swiss tables (`player_profiles`,
`tournament_participants`, `player_verifications`) stay. Link across DBs is
the `fide_id` string value, never a FK. Migration chain seeded from
`bb0160… + e7a2… + b3f9…` with the unapplied `b3f9…` explicitly approved +
rehearsed (BLOCKER). Row-count/growth/index decisions measured in Phase 2,
not guessed.

## 12. Deployment strategy

Own venv (Python 3.12 prod parity), own DB, env-only config, Passenger
entry mirroring Swiss (`passenger_wsgi.py` + `PassengerAppRoot` +
`tmp/restart.txt`), Cron `flask fide-worker` every few minutes (DB rows =
queue, no Celery/Redis), instance-anchored staging + 90-day retention,
`GET /healthz` + `GET /status`, nightly dumps + restore rehearsal
(`docs/09`). Sequence: provision → migrate → seed one period → shadow →
flagged cutover (C1/C11, C2/C3, C4/C5 last) → decommission Swiss write
paths + migrate history → harden. Never `reset_db` existing DBs; never
`db upgrade` without approval. No unverified production paths (all marked
`[CONFIRM]`).

## 13. Git/repository strategy

```text
G:\Programming\Web\swiss_app_dev\   independent repo (Swiss Manager)
G:\Programming\Web\fide_service\    independent repo (THIS repo)
```

No submodule, no mixed commits/histories. Small focused revertible commits;
inspect tree + staged diff pre-commit; never commit secrets/`.env`/venvs/
caches/generated files; never rewrite history or discard user work. Phase 0
= docs scaffolding commit(s) in `fide_service` only; Swiss untouched.

## 14. Extraction roadmap

Six vertical slices (`docs/11`): 1 skeleton+ported layers (no traffic) →
2 read API v1 + admin + seed import → 3 Swiss client + shadow dual-read →
4 flagged per-consumer cutover + edit-bug fix → 5 decommission Swiss writes
+ history migration → 6 hardening/ops. Each with objective, prerequisites,
affected systems, data movement, scope, tests, acceptance, rollback, risk.

## 15. Risks

Unapplied job migration run without approval; edit-bug wipe reaching
cutover unfixed; API-key issuance/rotation missing at traffic time;
shadow parity skipped (silent semantic drift in titles/ratings/history);
history migration without checksums; upstream format drift (parser is
tag-tolerant but federation filter is case-sensitive); quota/disk on new
host unverified; stale-period verification accepted. Mitigations are the
BLOCKERs + acceptance gates in `docs/11`/`docs/12`.

## 16. Open decisions

`DECIDED` (8, immutable without new approval) / `RECOMMENDED` (5, confirm
Phase 1) / `UNKNOWN` (6: search floor, staleness N, `birth_year`, prod
paths, row counts, upstream HTTP) / `BLOCKER` (4: migration approval,
edit-bug fix, key issuance, freeze/drop rehearsal). Full list: `docs/12`.
Genuine unknowns are left open — do not silently resolve.

## 17. Acceptance criteria (Phase 0 complete iff)

1. Current FIDE fully mapped (`01`). 2. Every significant Swiss→FIDE
   dependency identified (`02`). 3. Ownership explicit (`03`). 4. Target
   architecture documented (`09` + this §2). 5. REST v1 sufficient for
   implementation (`05`). 6. Import extraction documented (`07`).
7. Failure behavior documented (`08`). 8. Deployment documented (`09`).
9. Testing strategy documented (`10`). 10. Decisions classified (`12`).
11. This executive doc complete. 12. `fide_service` repo independently
    initialized. 13. Docs committed as focused commit(s). 14. Swiss repo
    untouched. — Do not begin Phase 1 after this; stop at committed docs.
