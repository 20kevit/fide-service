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
| `RATE_LIMIT_PER_MINUTE` | — | `600` | Per key per process; 0 disables (never in prod) |
| `AUTH_FAIL_LIMIT_PER_HOUR` | — | `120` | Rejected-auth budget per IP/hour; over-budget IPs get 429 pre-auth; 0 disables (never in prod) |
| `FIDE_UPLOAD_MAX_BYTES` | — | `125829120` | Admin upload cap |
| `FIDE_DOWNLOAD_BASE_URL` | — | `https://ratings.fide.com/download/` | Outbound only; HTTP override only if HTTPS provably fails (MITM risk) |
| `FIDE_CONNECT_TIMEOUT` / `FIDE_READ_TIMEOUT` / `FIDE_MAX_RETRIES` | — | `10` / `30` / `3` | Downloader bounds |
| Local dev only | — | — | `RATE_LIMIT_PER_MINUTE=0`, SQLite file |

## Monthly import (normal operation)

1. `flask --app run.py db-upgrade` (only when a new migration ships).
2. `POST /api/v1/admin/jobs/ensure {"period": "YYYY-MM"}` (admin key).
3. Cron every few minutes: `flask --app run.py fide-worker [--period YYYY-MM] [--timeout SECS]`.
4. Watch `GET /api/v1/admin/jobs?period=YYYY-MM` until
   `standard/rapid/blitz = completed`.
5. Confirm `GET /api/v1/status` shows the new `latest_period`.

## Gateway rate limiting (production-only human action)

The in-app limiters (`interfaces/api/auth.py`: per-key sliding window +
pre-auth per-IP rejected-attempt budget) are process-local by design —
under multi-process Passenger/LSAPI each worker counts independently, so
they are defense-in-depth, not a global cap. No shared store (Redis or
equivalent) exists on this stack, and none is introduced for this.

Required human action on the host/gateway (NOT in this repo, NOT applied):

1. Add a global per-IP connection/request throttle at the server or CDN
   layer (LiteSpeed server-level throttle rules, or a proxy such as
   Cloudflare Rate Limiting). Suggested starting point: well above
   legitimate use (the Swiss client makes a handful of reads per
   tournament operation), e.g. hundreds of requests/min/IP with a short
   block, plus a stricter rule for repeated 401/403 responses (key
   guessing tripwire).
2. Alert on `auth rejected` / `rate limit exceeded` log lines and on
   `failed` import jobs (see Logging/Monitoring below).

Until that exists, the app-side limiters plus LSAPI child limits are the
effective backstop (a ~650-request burst was observed to 503 at the
server with no app 429s — expected under per-process counting).

## Recovery guarantees (verified behavior, not aspirations)

### Worker recovery

* One tick claims and runs exactly ONE step, then exits. A second
  concurrent worker reports `busy`/`blocked` and changes nothing — a
  claimed job is never double-processed (row lock on MySQL; heartbeat
  check everywhere).
* The worker heartbeats on every state change, every import batch, and
  (at most every 10 s) during downloads. A step whose heartbeat is older
  than `IMPORT_TIMEOUT_SECONDS` (default 600; `--timeout SECS` overrides
  per invocation) is marked `failed/WORKER_STALE` by the next tick.
* Stale/failed steps NEVER auto-retry. Recover with ONE explicit retry:
  `flask --app run.py fide-retry --period YYYY-MM --type standard|rapid|blitz`
  (or `POST /api/v1/admin/jobs/retry`). Successors resume in
  `standard → rapid → blitz` order afterwards. `completed` is terminal
  and is never reprocessed.

### Import recovery

* Import commits per `IMPORT_BATCH_SIZE` (default 500). A crash leaves
  the committed prefix in place — this is intentional, not corruption.
* Before anything is deleted, the `verifying` stage requires
  `processed > 0` AND `stored > 0`, else the job fails with
  `INVALID_FIDE_FORMAT` and the archive is kept for diagnosis.
* Retrying a failed step reprocesses the same file idempotently
  (primary/unique-key upserts, last record wins) and converges to the
  same final counts with no duplicates. Proven by `tests/test_recovery.py`
  (mid-batch crash, commit-loss crash, verify/prune failure, double run).

### Filesystem recovery

* Downloads stream to `<type>.zip.part` and are atomically renamed only
  on success; failed downloads delete the `.part` — a partial file is
  never trusted as a dataset.
* A staged/consumed ZIP is deleted only after `completed`. Failed jobs
  keep their ZIP for diagnosis and retry.
* `tmp/<YYYY-MM>/` directories older than the retention window (90 days)
  are swept after each success and by `fide-cleanup`. Cleanup is
  best-effort: its failure warns but never flips a success to failed.

### Database recovery

* Repositories only flush; commits/rollbacks happen exclusively in the
  application import service at documented boundaries (claim, heartbeat,
  batch, completion, failure recording). Read paths never write.
* No downgrades: restore from backup instead (`db-upgrade` only moves
  forward; `db-status` shows applied vs pending).
* After any restore: re-run `fide-verify` and spot-check known players
  before declaring the dataset good.

### Operational recovery

* Detect: `GET /api/v1/admin/jobs?period=YYYY-MM` (states, heartbeats,
  `error_code`), `GET /api/v1/admin/verify` (`ok` must be true), the
  `fide_watch.sh` threshold mails, `worker-cron.log` (`OK` vs `IDLE` vs
  `ERROR` lines), `stderr.log` (tracebacks — there should be none).
* Retry safely: failed steps via explicit retry (above); never
  re-`ensure` a period to "fix" it (ensure is idempotent and changes
  nothing about failed steps).
* Escalate (manual intervention): repeated failures with the same
  `error_code` across retries (source/DB/disk problem), `verify` not
  `ok`, disk pressure `fide-cleanup` cannot clear, or database
  unreachable (reads fail closed with generic 500s, never partial data).

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
