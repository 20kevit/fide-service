# 14 — Phase 2 Evidence: MySQL Verification + Full-Scale Rehearsal

All local. No Beta/Production involvement. Official September ZIPs were
UNREACHABLE from this environment (ratings.fide.com connects but never
sends bytes; three attempts, HTTP+HTTPS, up to 60 s timeouts), so the
rehearsal used deterministic generator output at the exact documented
baseline scale and official XML format. Structural proof (streaming,
locking, integrity, plans) is complete; byte-identical official content
remains a deployment-phase re-run item.

## A. MySQL instance (local, dedicated)

- Engine: **MariaDB 12.3.3** (portable, TEMP dir, port 3307, loopback only)
- Database: `fide_rehearsal` (`utf8mb4/utf8mb4_unicode_ci`), app user
  `fide@127.0.0.1` — separate from every Swiss/Beta/Production schema.
- Driver: `pymysql==1.1.1` (note: the wheel reports `__version__ 1.4.6`;
  verified byte-identical to the official PyPI wheel — upstream quirk).
- Gated tests: `tests/test_mysql.py` (skip without `FIDE_TEST_MYSQL_URL`).

## B. Concurrency + transactions (MySQL, `tests/test_mysql.py`, 5 passed)

- commit/rollback/nested-savepoint behavior verified.
- Two racing workers, one step: exactly `["busy", "claimed:standard"]` —
  `SELECT ... FOR UPDATE` row locking proven behaviorally (no double
  ownership). Second worker blocks on the lock, then observes the fresh
  heartbeat and backs off.
- Stale recovery: aged heartbeat → `failed/WORKER_STALE` → explicit retry
  → `tick` succeeds on MySQL.

## C. Query plans (MariaDB EXPLAIN, all indexed except documented case)

| Query | Plan (key/type) |
|---|---|
| player by PK | `PRIMARY/const` |
| rating by UQ | `uq_fide_rating_period_type/const` |
| history range | `uq_fide_rating_period_type/ref` |
| latest GROUP BY | `ix_fide_ratings_lookup/range` (DB-side, never Python) |
| batch IN | `PRIMARY/range` |
| job claim select | `uq_fide_job_period_type/ref` |
| federation filter | `ix_fide_players_federation/ref` |
| name substring `%x%` | `None/ALL` — documented limitation (B-tree cannot serve a leading wildcard; fide_id-prefix + federation paths stay indexed) |

No change required: the one full scan is inherent to substring search and
bounded by `limit ≤ 100`.

## D. Rehearsal dataset (synthetic, official format, baseline scale)

- Generator: `scripts/generate_rehearsal_xml.py` (deterministic).
- standard 16,244 / rapid 26,830 / blitz 11,959 players; unique 32,925;
  IRI 6,585 (every 5th ID); blitz ⊆ rapid with real overlap into
  standard → 10,149 three-type players.
- Two periods imported (2026-08, 2026-09) to exercise accumulation +
  IRI-only history pruning.

## E. Rehearsal results (MySQL, `scripts/run_rehearsal.py`)

| Tick | Processed | Stored | Wall | Pruned |
|---|---|---|---|---|
| 2026-08 standard | 16,244 | 16,244 | 98.1 s | 0 |
| 2026-08 rapid | 26,830 | 26,830 | 158.6 s | 0 |
| 2026-08 blitz | 11,959 | 11,959 | 66.5 s | 0 |
| 2026-09 standard | 16,244 | 16,244 | 90.5 s | 12,995 |
| 2026-09 rapid | 26,830 | 26,830 | 149.6 s | 21,464 |
| 2026-09 blitz | 11,959 | 11,959 | 67.2 s | 9,567 |

- Totals: processed 110,066 / stored 110,066 / pruned 44,026.
- Final state: **32,925 players / 66,040 ratings**
  (Sep 55,033 + Aug IRI-only 11,007); by_type
  standard 19,493 / rapid 32,196 / blitz 14,351.
- Integrity (`admin/verify`): ok=true, no duplicates, no null IDs, no
  invalid types, no orphans, periods [2026-08, 2026-09].
- Known players (manifest): 4/4 exact (federation + per-type
  rating/games/k), incl. an all-three-types IRI player.
- Resources: wall 632 s total (~90–180 rows/s); **Python peak 3.6 MB**,
  **process peak RSS 81.3 MB** (2 GB server budget: comfortable);
  DB 29.5 MB; tmp bytes after run: **0** (archives deleted on success).
- API spot-checks on rehearsal data: player/search/batch/status/history
  all 200 with correct payloads and dataset blocks.

## F. Throughput note (no redesign)

~90–180 rows/s row-by-row ORM on loopback MySQL is reliability-first by
choice (idempotent upserts, heartbeat, bounded memory). A full global
file (larger than this IRI-scale rehearsal) means a proportionally
longer monthly step — acceptable for a background Cron job, with one
recorded RISK: a crash restarts the step from scratch (no mid-step
resume; rework is idempotent but costs time). FOLLOW-UP: resume-offset.
No importer redesign in this phase.
