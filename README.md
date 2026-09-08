# FIDE Service

> Status: **production-ready — frozen** (all audit phases complete, live dataset verified).

An independent, self-contained web service that imports the official monthly
FIDE rating lists and serves player identity, ratings, and rating history
over a versioned JSON API. Built with Flask + SQLAlchemy, running on
SQLite for local development and MySQL/MariaDB in production.

Swiss Manager (tournament management) is a *consumer* of this service over
HTTP — it is not a dependency. This repository shares no code, models,
database, configuration, or Git history with any other project (verified by
whole-repo import grep; see `docs/13-phase1-decisions.md`).

## Key features

* **Player identity** — FIDE ID (primary key), name, federation, sex,
  titles (`title`/`wtitle`/`otitle`/`foatitle` preserved verbatim),
  birth year, inactive flag.
* **Ratings** — Standard, Rapid, and Blitz snapshots per monthly period
  (`rating`, `games`, `k_factor`), plus ascending per-type history.
* **Search** — bounded player search (numeric prefix or name terms, optional
  federation filter, `limit ≤ 100`).
* **Batch lookup** — up to 100 IDs per request with explicit `found`/`missing`.
* **Dataset transparency** — every data response carries
  `{period, imported_at, source}` so consumers never infer staleness.
* **Monthly import pipeline** — download (or staged upload) → inspect →
  validate → stream-import → verify → prune → cleanup, one step per worker
  tick, `standard → rapid → blitz` ordering, idempotent upserts, explicit
  retries, stale-worker recovery.
* **Data verification** — counts, per-type distribution, duplicate keys,
  empty IDs, invalid types, orphan ratings, period coverage.
* **Security** — API-key auth with a separate admin credential,
  constant-time comparison, fail-closed behavior, per-key and pre-auth
  rate limiting, security event logging, XML/ZIP hardening, sanitized
  error envelope with request IDs.

## Architecture

Clean Architecture / DDD layering, verified by static import analysis
(no cycles, no back-edges):

```text
interfaces/  →  application/  →  domain/  ←  infrastructure/
(Flask API,       (orchestration,  (pure Python:   (SQLAlchemy models,
 CLI worker)       transactions,    parser, rules,   flush-only repos,
                    no Flask)       errors)          storage, downloader)
```

Dependency direction: `interfaces → application → domain ← infrastructure`.
Repositories are flush-only; commits and rollbacks live exclusively in the
application import service. The domain layer imports nothing but the
standard library and sibling domain modules.

## Repository structure

```text
application/     player queries, import orchestration, dataset checks
domain/          pure-Python models, XML parser, validation, titles, errors
infrastructure/  database handle, ORM models, repositories, ZIP storage,
                 downloader, owned migration runner
interfaces/      Flask app factory, REST API v1 blueprints, CLI commands
migrations/      versioned migration history (schema_migrations-tracked)
scripts/         full-scale rehearsal tooling (local MySQL, manual use)
tests/           domain / parser / repository / API / import / security /
                 recovery / performance / integrity suites + fixtures
docs/            architecture decision records, API contract, runbook,
                 deployment checklist, capacity + integrity evidence
deployment/      server-configuration SAMPLES (documentation only)
config.py        environment-driven settings (secrets never defaulted)
run.py / wsgi.py local runner / WSGI application object
```

## API (base `/api/v1`, JSON only)

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | none | Liveness probe → `{"ok": true}` |
| `GET /status` | read key | Players/ratings/periods/jobs overview |
| `GET /players/{id}[?include=ratings\|none]` | read key | Identity + latest ratings + dataset block |
| `GET /players/search?q=&federation=&limit=` | read key | Bounded search (`q` ≥ 2 chars, `limit` ≤ 100) |
| `GET /players/{id}/ratings[?type=&period=]` | read key | Latest or one-period snapshot |
| `GET /players/{id}/ratings/history[?type=&from=&to=&limit=&offset=]` | read key | Ascending history (`limit` ≤ 1000) |
| `POST /players/batch` `{"ids": [...]}` | read key | Bulk lookup, max 100, partial `missing` tolerated |
| `POST /admin/jobs/ensure` | admin key | Create pending jobs for a period (→ 202) |
| `GET /admin/jobs[?period=&limit=]` | admin key | Job states, heartbeats, sanitized errors |
| `POST /admin/jobs/retry` | admin key | Re-queue one FAILED step |
| `POST /admin/uploads` | admin key | Stage a monthly ZIP (→ 201, with filename/period verdict) |
| `GET /admin/verify[?period=]` | admin key | Counts + integrity report |

Read endpoints take the key in `X-API-Key`; admin endpoints take
`X-Admin-Key` (or the read key when no separate admin key is configured —
a valid read key on an admin endpoint returns `403`, not `401`).
Missing/invalid credentials → `401`; unconfigured service → `503`.
Errors use a stable envelope
`{error: {code, message, details, request_id}}` with codes
`BAD_REQUEST / UNAUTHORIZED / FORBIDDEN / NOT_FOUND / CONFLICT /
RATE_LIMITED / SERVICE_UNAVAILABLE / INTERNAL` — never tracebacks, DB
errors, or secrets.

```powershell
$h = @{"X-API-Key" = "YOUR_API_KEY"}
Invoke-RestMethod http://127.0.0.1:5001/api/v1/players/12512294 -Headers $h
```

Full frozen contract: [`docs/API_V1.md`](docs/API_V1.md).

## Authentication & security

* Service-to-service API keys from the environment only; `hmac.compare_digest`;
  fail closed (401/503); admin/read credential separation with 403 distinction.
* Rate limiting: per-key sliding 60 s window (default 600/min) plus a
  pre-auth per-IP rejected-attempt budget (default 120/hour) — both
  process-local and bounded (defense-in-depth; see runbook for gateway caps).
* Safe security-event logging (fingerprints, never credentials), minimal
  response headers (`nosniff`, `no-referrer`; HSTS/redirect at the gateway).
* FIDE source defaults to HTTPS with explicit certificate verification;
  staged-upload path as the higher-assurance alternative.
* XML/ZIP hardening: `DOCTYPE`/`ENTITY` rejection across the whole stream,
  HTML-disguised-as-XML rejection, traversal-safe archives, streaming parse
  (never full-file load), validation before any database write.

## FIDE data import pipeline

```text
ensure → download (or staged) → inspect → validate → import → verify
       → prune → cleanup → completed
```

* Rating types `standard → rapid → blitz`, strictly ordered; one worker tick
  (`flask --app run.py fide-worker [--period YYYY-MM]`) claims and runs
  exactly one step, designed for cron every few minutes. The web process
  never spawns import threads.
* Job states: `pending → downloading → validating → importing → verifying →
  completed`, any step → `failed`. Heartbeats guard liveness; expired
  heartbeats become `failed/WORKER_STALE`; only explicit retry re-queues
  (`failed → pending`); `completed` is terminal.
* Streaming import in `IMPORT_BATCH_SIZE` batches (default 500) with bounded
  memory; idempotent upserts (primary/unique-key, last record wins), so any
  retry converges to identical counts with no duplicates.
* Verification gate (`processed > 0` and `stored > 0`) runs before pruning or
  deletion; failures keep the archive for diagnosis, successes delete it.
* Scope: identity + current ratings for ALL federations; older periods are
  pruned for federations outside `HISTORY_FEDERATIONS` (default `IRI`).

## Database

* SQLite for local development, MySQL/MariaDB (`mysql+pymysql`) in production.
* Three tables, owned schema, no shared/legacy mirrors:
  * `fide_players` — `fide_id` PK; identity columns; name/title indexes.
  * `fide_ratings` — `(fide_id, period, rating_type)` unique; FK to players;
    lookup index `(fide_id, rating_type, period)` serving latest-per-type
    reads in the database.
  * `fide_import_jobs` — `(period, rating_type)` unique; state machine,
    heartbeats, progress counters, classified error metadata.
* Owned migration runner (`schema_migrations` table; `db-upgrade` /
  `db-status` CLI); downgrades intentionally unsupported — restore from
  backup instead. Foreign keys enforced on both engines.

## Reliability & recovery

* Atomic single-step claims (row locking where supported); concurrent ticks
  report busy/blocked and change nothing.
* Partial batch commits are intentional: crash recovery reprocesses
  idempotently; covered by 33 dedicated recovery tests (claim/commit/prune/
  verify/cleanup boundaries, stale mechanics, downloader failures,
  filesystem leftovers, DB-down API behavior).
* `fide-verify` (CLI) and `GET /admin/verify` fail closed on duplicates,
  nulls, invalid types, orphans, and empty imports.

## Performance & capacity (measured)

Labeled per `docs/15-performance-capacity.md`: production server-side
lookup ≈34 ms; worst-case (zero-match) search ≈538 ms at ~786k rows;
import ≈1.5 h per global month at ~90–180 rows/s; import memory peak
single-digit MB; batch size 500 kept after a flat 100/500/2000 comparison.
Per-endpoint SQL counts are pinned by regression tests (`tests/test_performance.py`).

## Testing

`pytest` — domain, parser, repository, API, import, security, recovery,
performance, integrity suites plus MySQL-gated concurrency tests:

```powershell
.\venv\Scripts\python.exe -m pytest tests -q
```

Current verified status: **202 passed, 5 skipped** (skips require a local
MySQL URL via `FIDE_TEST_MYSQL_URL`). Tiny fixtures only — the official
multi-hundred-MB file is never needed for tests.

## Local development

Requires Python 3.12+ (production runs 3.12).

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # then set FIDE_API_KEY (required to serve)
.\venv\Scripts\python.exe -m flask --app run.py db-upgrade
.\venv\Scripts\python.exe -m flask --app run.py fide-ensure-jobs --period 2026-09
.\venv\Scripts\python.exe -m flask --app run.py fide-worker --period 2026-09
.\venv\Scripts\python.exe run.py   # serves API on 127.0.0.1:5001
```

## Configuration

All settings come from the environment (see [`.env.example`](.env.example);
copy to `.env`, never commit it). Key variables:

| Variable | Default | Notes |
|---|---|---|
| `FLASK_ENV` / `SECRET_KEY` | `development` / — | Both required in production (fail fast) |
| `DATABASE_URL` | `sqlite:///fide.db` | Production: `mysql+pymysql://…?charset=utf8mb4` |
| `FIDE_API_KEY` | — (required to serve) | Read credential; unconfigured → 503 |
| `FIDE_ADMIN_KEY` | — (= read key) | Set a distinct value in production |
| `FIDE_DATA_DIRECTORY` | `./var/fide-data` | Service-writable staging root |
| `IMPORT_TIMEOUT_SECONDS` | `600` | Heartbeat staleness |
| `IMPORT_BATCH_SIZE` | `500` | Commit cadence (benchmarked, keep) |
| `HISTORY_FEDERATIONS` | `IRI` | Comma list; empty keeps all history |
| `RATE_LIMIT_PER_MINUTE` | `600` | Per key; 0 disables (never in prod) |
| `AUTH_FAIL_LIMIT_PER_HOUR` | `120` | Pre-auth IP budget; 0 disables (never in prod) |
| `FIDE_UPLOAD_MAX_BYTES` | `125829120` | Admin upload cap |
| `FIDE_DOWNLOAD_BASE_URL` | `https://ratings.fide.com/download/` | Outbound only |
| `FIDE_CONNECT_TIMEOUT` / `FIDE_READ_TIMEOUT` / `FIDE_MAX_RETRIES` | `10` / `30` / `3` | Downloader bounds |

## Production deployment

General model (no project-specific secrets involved): a Passenger/LiteSpeed
(cPanel-style) host serves the app via a `passenger_wsgi.py` entry that loads
`wsgi.py`; configuration arrives as environment variables; MySQL is
provisioned separately; a cron invokes `fide-worker` every few minutes; the
monthly flow is ensure → ticks → verify → status. Server-configuration
*samples* (never live config) live in `deployment/`; the operational
runbook is [`docs/RUNBOOK.md`](docs/RUNBOOK.md) and the pre-deploy checklist
is [`docs/PRODUCTION_CHECKLIST.md`](docs/PRODUCTION_CHECKLIST.md).

## Documentation

* [`docs/API_V1.md`](docs/API_V1.md) — frozen v1 contract
* [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — operations, recovery guarantees, gateway actions
* [`docs/13-phase1-decisions.md`](docs/13-phase1-decisions.md) — build decisions and divergences
* [`docs/14-phase2-evidence.md`](docs/14-phase2-evidence.md) — MySQL + rehearsal evidence
* [`docs/15-performance-capacity.md`](docs/15-performance-capacity.md) — measured capacity model
* [`docs/16-data-integrity.md`](docs/16-data-integrity.md) — invariants, policies, retention, restore
* [`docs/01`-`12`](docs/01-current-state.md) + `FIDE_EXTRACTION_ARCHITECTURE.md` — original extraction analysis (historical reference)

## Project status

**Production-ready — frozen.** All audit phases (discovery, architecture,
security, reliability, performance, integrity) are complete with a live
dataset verified. No feature development is planned; changes, if any, go
through the same audit → test → controlled-deploy discipline documented above.

## License

No license has currently been specified for this repository.
