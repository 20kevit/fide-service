# 09 — Deployment: Independent FIDE Service

Based on verified Swiss deployment facts (`passenger_wsgi.py:1-7`,
`run.py:1-9`, `.htaccess.example:28-30`, `docs/DEPLOYMENT.md`, `config.py`,
`requirements.txt`, `app/__init__.py:43-65`, `app/cli.py:26-54`).
Do not invent unverified production paths — placeholders below are marked
`[CONFIRM]`.

## 1. Application structure (RECOMMENDED FUTURE, derived from current layers)

```text
fide_service/
├── run.py / passenger_wsgi.py      # entry points (mirrors Swiss pattern)
├── config.py                       # env-driven config (no secrets in repo)
├── requirements.txt                # Flask 3.1.x, Flask-SQLAlchemy 3.1.x,
│                                   #   Flask-Migrate 4.1.x, requests, dotenv…
├── app/                            # Flask factory (create_app)
├── domain/fide/                    # parser, validation, errors, models (moved verbatim)
├── application/fide/               # job service, search service (sole pipeline)
├── infrastructure/fide/            # storage, downloader, diagnostics (moved verbatim)
├── infrastructure/models/          # fide_* models only
├── infrastructure/repositories/    # fide_* repositories only
├── interfaces/api/                 # REST v1 blueprints (read + separated admin)
├── migrations/                     # FIDE-only Alembic chain (seeded from §4)
├── data/fide/<YYYY-MM>/            # instance-anchored staging (never CWD)
├── tests/                          # per docs/10-testing-strategy.md
└── docs/                           # this workspace (kept)
```

## 2. Runtime

* Python: **3.12** (Swiss `docs/DEPLOYMENT.md:21-30` canonical;
  `PassengerPython → <home>/python312_app_venv/bin/python3` in
  `.htaccess.example`; dev observed 3.14.5 — pin 3.12 for prod parity).
* Virtualenv: own venv (e.g. `[CONFIRM] <home>/fide_service_venv`),
  `pip install -r requirements.txt`. Separate from Swiss venv.
* Entry: `passenger_wsgi.py` (`sys.path.insert(0, PROJECT_ROOT); from run
  import app as application`) + `PassengerAppRoot "[CONFIRM]"`,
  `PassengerStartupFile passenger_wsgi.py`. Restart via `tmp/restart.txt`
  (Swiss pattern).
* No threads/Celery/Redis (CURRENT has none; keep none — Cron + DB jobs).

## 3. Database

* Own database (own `DB_NAME`, e.g. `[CONFIRM] fide_db`), MySQL
  `mysql+pymysql://…?charset=utf8mb4` in prod (`pool_pre_ping`,
  `recycle 280`, small pool `size 5 / overflow 2` as today), SQLite file
  for local dev. Fail-fast if `DB_NAME`/`SECRET_KEY` missing in production
  (mirror `config.py:17-41`).
* Migrations: FIDE-only chain seeded from current FIDE DDL
  (`bb0160…` FIDE tables + `e7a2…` columns + `b3f9…` jobs table).
  The unapplied `b3f9…` job-table migration must be explicitly approved and
  rehearsed before first deploy (see `11-extraction-plan.md`).
* Backup: nightly MySQL dump of the FIDE DB + retention policy
  `[CONFIRM]`; restore rehearsal before cutover.

## 4. Environment variables (names only; values never committed)

| Var | Purpose | Default today |
|---|---|---|
| `FLASK_ENV` / `SECRET_KEY` | env + signing | dev-random / prod-required |
| `DB_USER / DB_PASSWORD / DB_HOST / DB_NAME` | own DB | prod-required |
| `FIDE_API_KEY` (new) | service-to-service auth (Swiss presents) | `[CONFIRM — generate, no default]` |
| `FIDE_ADMIN_KEY` (new, RECOMMENDED) | separated admin scope | `[CONFIRM]` |
| `FIDE_DOWNLOAD_BASE_URL` | upstream archives | `http://ratings.fide.com/download/` |
| `FIDE_ALLOWED_FEDERATIONS` | import filter | `["IRI"]` (keep until product decision) |
| `FIDE_DATA_DIR` | staging root (instance-anchored) | `<instance>/data/fide` |
| `FIDE_RAW_RETENTION_DAYS` | file retention | `90` |
| `FIDE_UPLOAD_MAX_BYTES` | admin upload cap | `120 MB` |
| `FIDE_CONNECT_TIMEOUT / FIDE_READ_TIMEOUT / FIDE_MAX_RETRIES` | downloader | `10 / 30 / 3` |
| `FIDE_WORKER_TIMEOUT_SECONDS` | heartbeat staleness | `600` |

`MAX_CONTENT_LENGTH` derived as `max(8MB, FIDE_UPLOAD_MAX_BYTES)` (current
`app/__init__.py:38-42` pattern) if the service keeps upload ingestion.

## 5. Cron / workers

* Cron (cPanel-style, `[CONFIRM]` schedule — CURRENT assumption: every few
  minutes): `flask fide-worker [--period YYYY-MM] [--timeout SECS]`
  (renamed from `fide-import-step`; semantics unchanged: one eligible step
  per invocation, `OK / IDLE / ERROR` exit lines, safe concurrent).
* No scheduler lib; DB rows are the queue (`claim_next_job` /
  `fail_stale_jobs` / `retry_job`). Monitor for `failed` / `WORKER_STALE`
  and alert (log + status endpoint), never auto-restart.

## 6. Filesystem / logs / health

* Writable roots (service-owned, Passenger-writable, never CWD):
  `<instance>/data/fide/<YYYY-MM>/` + `tmp/<YYYY-MM>/<type>.zip` +
  staged uploads; retention sweeps via `cleanup_old_files`.
  `[CONFIRM]` production path/quota (Swiss comments cite ~1.5 GB quota
  driving stream-only design — re-confirm for the new host).
* Logs: app log + worker log (names/paths `[CONFIRM]`); no secrets, no
  key material, `request_id` correlation.
* Health: unauthenticated `GET /healthz` (LB) + authenticated
  `GET /api/v1/status` (Swiss + monitoring). Backup verification included
  in smoke tests.

## 7. Deployment sequence (summary; full plan in `11-extraction-plan.md`)

1. Provision host/venv/DB/env/Cron (no traffic). 2. Deploy service +
   run FIDE-only migrations. 3. Seed/import one period, verify
   `status` + API reads. 4. Point Swiss staging client at service with
   dual-read/shadow verification. 5. Cut over reads per consumer (C1→C5,
   C11), keeping snapshots as fallback. 6. Retire Swiss FIDE write paths
   only after parity + rollback rehearsal. Never `reset_db` on an existing
   DB; never run `db upgrade` without explicit approval.
