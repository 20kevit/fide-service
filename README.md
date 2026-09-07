# FIDE Service — Independent Bounded Context (Phase 1, local)

> Status: **runnable independent Flask service (local development).**
> Phase 0 architecture docs live in `docs/01–12 + FIDE_EXTRACTION_ARCHITECTURE.md`;
> Phase 1 build decisions are recorded in `docs/13-phase1-decisions.md`.
> No deployment, no cutover, no Swiss changes — local only.

## Repository relationship

```text
G:\Programming\Web\
├── swiss_app_dev\      ← Swiss Manager (independent Git repository, untouched)
└── fide_service\       ← FIDE Service (THIS repository, independent Git history)
```

This service never imports Swiss Manager code, models, database, or
files — and Swiss Manager does not import this service (verified by grep;
see `docs/13-phase1-decisions.md`).

## Quick start (local only)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env   # then set FIDE_API_KEY (required to serve)
.\venv\Scripts\python.exe -m flask --app run.py db-upgrade
.\venv\Scripts\python.exe -m flask --app run.py fide-ensure-jobs --period 2026-09
.\venv\Scripts\python.exe -m flask --app run.py fide-worker --period 2026-09
.\venv\Scripts\python.exe run.py   # serves API on 127.0.0.1:5001
.\venv\Scripts\python.exe -m pytest tests -q
```

Authenticated example (headers, never URLs/bodies for secrets):

```powershell
$h = @{"X-API-Key" = $env:FIDE_API_KEY}
Invoke-RestMethod http://127.0.0.1:5001/api/v1/players/12512294 -Headers $h
```

## Layout (Clean Architecture / DDD)

```text
interfaces/      Flask factory, REST API v1, CLI worker  → depends on application
application/     orchestration + transactions (no Flask) → depends on domain
domain/          pure Python (parser, validation, titles, errors)
infrastructure/  SQLAlchemy models, flush-only repos, ZIP storage,
                 downloader, owned migration runner      → implements persistence
migrations/      owned versioned history (0001_initial)
tests/           domain, parser, repository, API, import, security suites
docs/            Phase 0 architecture + Phase 1 decisions
config.py        env-driven settings (secrets never defaulted)
run.py / wsgi.py local runner / future Passenger entry (inert for now)
```

Dependency direction: `interfaces → application → domain ← infrastructure`.

## API v1 (base `/api/v1`, key in `X-API-Key`, fail closed)

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Unauthenticated liveness probe |
| `GET /status` | Players/ratings/periods/jobs overview |
| `GET /players/{id}[?include=ratings]` | Identity + latest ratings + dataset period |
| `GET /players/search?q=&federation=&limit=` | Bounded search (q ≥ 2 chars, limit ≤ 100) |
| `GET /players/{id}/ratings[?type=&period=]` | Latest or one-period snapshot |
| `GET /players/{id}/ratings/history[?type=&from=&to=&limit=&offset=]` | Ascending history |
| `POST /players/batch` `{"ids": [...]}` | Bulk lookup, max 100, partial `missing` tolerated |
| `POST /admin/jobs/ensure` | Create pending jobs (separate admin credential) |
| `GET /admin/jobs[?period=]` | Job states, heartbeats, errors |
| `POST /admin/jobs/retry` | Re-queue one FAILED step |
| `POST /admin/uploads` | Stage a monthly ZIP (worker prefers staged files) |
| `GET /admin/verify[?period=]` | Counts + integrity report |

Errors: `{error: {code, message, details, request_id}}` with stable codes
(`BAD_REQUEST/UNAUTHORIZED/FORBIDDEN/NOT_FOUND/CONFLICT/RATE_LIMITED/
SERVICE_UNAVAILABLE/INTERNAL`). No tracebacks, no DB errors, no secrets.

## Import model (job-based, the only pipeline)

`ensure → download (or staged) → validate → import (streaming, batches of
500) → verify → prune non-configured-federation history → cleanup →
completed`, ordered `standard → rapid → blitz` with atomic claims,
heartbeats, `WORKER_STALE` recovery, and explicit-only retry. Cron runs
`flask --app run.py fide-worker [--period YYYY-MM]` every few minutes;
the web process never spawns import threads.

Data scope: identity + current ratings for ALL federations; multi-period
history retained per `HISTORY_FEDERATIONS` (default `IRI`), extendable
without redesign.

## Safety (Phase 1 guarantees)

* Beta/production untouched; no deployment performed.
* Swiss Manager code and database untouched (no migration, no shared models).
* No secrets committed (`.env`/venvs/caches/ZIPs gitignored).
* Unit tests use tiny fixtures — the 100+ MB official file is never needed.
