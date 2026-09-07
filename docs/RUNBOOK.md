# FIDE Service — Operational Runbook (draft, deployment phase owns reality)

## Production environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `FLASK_ENV` | — | `development` | `production` enables fail-fast checks |
| `SECRET_KEY` | prod | — (dev only: random) | No committed default, ever |
| `DATABASE_URL` | prod | `sqlite:///fide.db` | prod: `mysql+pymysql://…?charset=utf8mb4` |
| `FIDE_API_KEY` | prod | — | Read credential; unconfigured → 503 |
| `FIDE_ADMIN_KEY` | — | — (= read key) | Set a distinct value in production |
| `FIDE_DATA_DIRECTORY` | — | `./var/fide-data` | Service-writable, survives restarts |
| `IMPORT_TIMEOUT_SECONDS` | — | `600` | Heartbeat staleness |
| `IMPORT_BATCH_SIZE` | — | `500` | Commit cadence (keep unless measured) |
| `HISTORY_FEDERATIONS` | — | `IRI` | Comma list; empty = keep all history |
| `RATE_LIMIT_PER_MINUTE` | — | `600` | Per key; 0 disables (never in prod) |
| `FIDE_UPLOAD_MAX_BYTES` | — | `125829120` | Admin upload cap |
| `FIDE_DOWNLOAD_BASE_URL` | — | `http://ratings.fide.com/download/` | Outbound only |
| `FIDE_CONNECT_TIMEOUT` / `FIDE_READ_TIMEOUT` / `FIDE_MAX_RETRIES` | — | `10` / `30` / `3` | Downloader bounds |
| Local dev only | — | — | `RATE_LIMIT_PER_MINUTE=0`, SQLite file |

## Monthly import (normal operation)

1. `flask --app run.py db-upgrade` (only when a new migration ships).
2. `POST /api/v1/admin/jobs/ensure {"period": "YYYY-MM"}` (admin key).
3. Cron every few minutes: `flask --app run.py fide-worker [--period YYYY-MM]`.
4. Watch `GET /api/v1/admin/jobs?period=YYYY-MM` until
   `standard/rapid/blitz = completed`.
5. Confirm `GET /api/v1/status` shows the new `latest_period`.

## Failure handling

| Symptom | Action |
|---|---|
| job `failed` (download/HTTP) | Inspect `error_code/detail`; retry later or stage ZIP via `POST /admin/uploads`, then `POST /admin/jobs/retry`. |
| job `failed/WORKER_STALE` | Worker died mid-step; explicit `retry` is safe (idempotent upserts). |
| `failed_present` blocks successors | Retry the failed predecessor; successors resume in order. |
| `429` from upstream | Back off; worker retries transient failures with backoff. |
| `401` spike on API | Rotate `FIDE_API_KEY` (change env + restart + update Swiss env). |
| `503` on all reads | Key unconfigured or dataset empty — check env + import status. |
| Disk pressure | `fide-cleanup` sweeps tmp dirs past retention; success already deletes consumed ZIPs. |

## Verification

- `flask --app run.py fide-verify [--period YYYY-MM]` and
  `GET /api/v1/admin/verify`: expect `ok=true`, zero duplicates/
  orphans/invalid types, sane per-type counts vs the manifest scale.
- After any restore: re-run verify + spot-check known players.

## Backup / restore

- Nightly MySQL dump of the FIDE database (deployment phase to
  schedule); retain per hosting policy.
- Restore rehearsal before cutover; re-run `fide-verify` after restore.
- Never run downgrades; restore from backup instead.

## Logging

Log endpoint, `request_id`, `fide_id`, latency, outcome. Never log keys,
bodies, or stack traces to clients (500 stays generic). Admin mutations
are audit-logged (period, type, outcome).
