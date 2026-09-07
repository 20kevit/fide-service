# Production Readiness Checklist — `fide.chessmanager.ir` (NOT executed)

Deployment is OUT OF SCOPE for Phase 2. Each item is marked:

- `READY` — proven locally in this phase
- `NOT READY` — needs implementation before deployment
- `REQUIRES HUMAN ACTION` — needs credentials/access/decisions on the host

| # | Item | Status | Note |
|---|---|---|---|
| 1 | DNS (`fide.chessmanager.ir`) | REQUIRES HUMAN ACTION | cPanel DNS + vhost |
| 2 | HTTPS (TLS cert, HSTS) | REQUIRES HUMAN ACTION | cPanel AutoSSL or equivalent |
| 3 | API key issuance (read + admin, distinct) | REQUIRES HUMAN ACTION | Generate, store in host env only |
| 4 | API key rotation procedure | NOT READY | Runbook documents rotation; rehearse once |
| 5 | MySQL credentials + database creation | REQUIRES HUMAN ACTION | Own schema, `utf8mb4`, least-privilege user |
| 6 | Filesystem permissions (`FIDE_DATA_DIRECTORY`, instance) | REQUIRES HUMAN ACTION | Passenger-writable, survives restarts |
| 7 | Cron (`fide-worker` every few minutes) | REQUIRES HUMAN ACTION | cPanel Cron, flake8-free single-step semantics |
| 8 | Passenger (`PassengerAppRoot`, `wsgi.py`, venv, `restart.txt`) | REQUIRES HUMAN ACTION | Mirror Swiss pattern, Python 3.12 venv |
| 9 | Timeouts (connect/read/worker) | READY | Defaults verified; tune from first imports |
| 10 | Rate limits (600/min/key) | READY | Code + 429 verified; tune from traffic |
| 11 | Logging (no secrets, request_id) | READY | Implemented; host log path TBD by humans |
| 12 | Monitoring (`/healthz`, `/status`, failed-job alert) | NOT READY | Endpoints exist; alerting hookup is human work |
| 13 | Backup (nightly dump + retention) | NOT READY | Schedule + restore rehearsal required |
| 14 | Rollback (previous build + DB dump restore) | NOT READY | Rehearse with the first deploy |
| 15 | Migration procedure (`db-upgrade` gated) | READY | Runner + `db-status`; never auto-run |
| 16 | Official-data re-import on host | REQUIRES HUMAN ACTION | Host must reach ratings.fide.com (unreachable from dev env) |
| 17 | Swiss cutover plan | NOT READY | Next phase: client flags → shadow → gradual |
| 18 | Response caching (ETag/Cache-Control) | NOT READY | Deferred, additive under v1 |
