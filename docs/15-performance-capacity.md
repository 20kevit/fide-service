# 15 — Performance & Capacity (Phase 5, measured)

Labels: `MEASURED` (executed benchmark), `ESTIMATED` (extrapolated,
marked as such), `UNVERIFIED` (not measurable from here),
`OPERATOR-DEPENDENT` (host-specific).

Reference dataset: 785,986 players / 1,355,048 ratings (2026-09,
production MySQL/MariaDB). Reference code: Phase 5+ (`get_dataset`
single-call fix included).

## 1. API latency (MEASURED)

Server-side on production (loopback MySQL, `fide_cli.sh shell`):

| Endpoint | Latency |
|---|---|
| player lookup (PK + latest) | 33.6 ms |
| search, zero-match (full-scan worst case) | 537.8 ms |

Local 100k-row SQLite rig (best-of-5, loopback, for shape comparison):

| Endpoint | Queries | Best |
|---|---|---|
| `GET /`, `/healthz` | 0 | ~1 ms |
| player lookup | 4 | 2.3 ms |
| search, common term (early-out at LIMIT) | 4 | 3.9 ms |
| search, rare term (full scan) | 4 | 64.3 ms |
| search, numeric prefix (index range) | 4 | 6.1 ms |
| ratings / history | 4 | ~2 ms |
| batch-100 | 4 | 5.6 ms |
| status | 8 | 60.9 ms |

Query counts are enforced by `tests/test_performance.py` — any increase
fails the suite. The rare-term search scales with table size (see §3);
everything else is index-served and flat.

## 2. Query plans (MEASURED, production MariaDB EXPLAIN)

| Query | Plan |
|---|---|
| player by PK | `PRIMARY`, 1 row |
| search name | full scan in PK order, early-out at `LIMIT 20` (`rows=20`) |
| latest GROUP BY per player | UQ index, temp/filesort over 1 row |
| history range | `ref` on UQ, 1 row |
| batch IN (3 IDs) | `PRIMARY range`, 3 rows |
| status `GROUP BY rating_type` | index scan ~1.3M rows + temp/filesort |

No missing-index finding: every selective path uses an index. The two
full scans (search-miss, status rollup) are inherent to their semantics
and bounded (LIMIT 100 / monitoring cadence).

## 3. Search verdict (MEASURED)

Worst case (term matching nothing): ~64 ms at 100k rows SQLite,
~538 ms at 786k rows production MySQL — linear in table size, as
expected for a leading-wildcard scan. Common terms exit early at LIMIT
(≈4 ms). Authenticated, `limit ≤ 100`, per-process rate-limited.
Verdict: acceptable, no rewrite (reaffirms the L1 decision, now with
production numbers). Revisit only on slow-query-log evidence.

## 4. Import throughput (MEASURED)

* Production MySQL, September 2026 (Phase 2 evidence): standard 566,830
  rows / 2448 s, rapid 473,539 / 1930 s, blitz 314,682 / 1250 s —
  ≈90–180 rows/s row-by-row ORM, reliability-first by design.
* Local SQLite batch-size comparison, 20k rows: batch=100 → 223 rows/s,
  batch=500 → 197 rows/s, batch=2000 → 211 rows/s; Python peak ≤2.9 MB
  in all cases. Throughput is flat across batch sizes (per-row ORM cost
  dominates; commit cadence is noise).
* Verdict: `IMPORT_BATCH_SIZE=500` stays. Larger batches buy no speed
  and coarsen recovery granularity + heartbeat cadence.

## 5. Memory (MEASURED)

* Import: streaming `iterparse` + per-batch `expunge_all`; peak Python
  ≤3.6 MB at rehearsal scale, ≤2.9 MB at 20k-row local runs; process
  RSS 81 MB against a 2 GB budget. XML is never fully loaded;
  ZIP members stream without extraction.
* API: bounded result sets (search ≤100, batch ≤100, history ≤1000);
  no unbounded collections in request paths.
* Rate limiter: ~2.2 µs/check overhead; stores bounded (5000 keys/IPs
  with stale purge).

## 6. Worker / cron capacity (MEASURED + ESTIMATED)

* One monthly period = 3 ticks (standard → rapid → blitz). MEASURED
  production wall times: ≈41 + 32 + 21 min ≈ 94 min total at global
  scale. Cron every 5 min drains this in ~19 invocations — ESTIMATED
  comfortable headroom (~40×) for the monthly cadence.
* Ticks are idempotent and single-step; overlapping invocations report
  busy/blocked and change nothing (tested, including MySQL locking in
  Phase 2). A crash restarts the step from scratch (no resume-offset;
  accepted, documented in `docs/14-phase2-evidence.md`).

## 7. Passenger / LiteSpeed capacity (MEASURED + OPERATOR-DEPENDENT)

* Observed: LSAPI `max children 6 … busy: 8` warnings on import day;
  no app processes persist for the service user (spawn-on-demand).
* `var/` steady at ~28 KB; no temp accumulation (`fide-data/tmp`
  empty after success; 90-day retention sweep verified).
* A ~650-request burst previously 503'd at the server with no app
  429s — expected under per-process counting (see RUNBOOK gateway
  section). True global caps remain a server/CDN human action.
* Health-check network sample: ~5 s end-to-end (high variance,
  network-dominated; server-side lookup itself is ~34 ms).

## 8. Capacity model (ESTIMATED unless noted)

* Dataset: MEASURED 786k players / 1.36M ratings / 431 MB DB.
  Monthly growth: UNVERIFIED (no multi-month production series yet) —
  re-measure after 2–3 import cycles.
* Import duration: ~1.5 h at current global scale; scales linearly
  with official file size (row-by-row ORM).
* API concurrency: single requests are millisecond-scale server-side;
  concurrency ceiling is the LSAPI child limit (OPERATOR-DEPENDENT),
  not the application.
* Disk: DB ~431 MB + headroom for one monthly growth increment;
  tmp peaks at one staged ZIP per (period, type) plus `.part` files,
  all cleaned on success.
* Warning thresholds (operational): status latency climbing past ~2 s
  (rollup strain), search-miss past ~2 s (table growth), worker tick
  exceeding 3 h per type, `var/` growth past 1 GB, LSAPI 503 recurrence
  outside bursts.

## 9. Methodology

Local rigs: `tests/test_performance.py` (deterministic query counts),
100k-row synthetic SQLite DB (bulk insert, mixed names/federations),
20k-row XML import at batch 100/500/2000 with `tracemalloc`, 20k-iteration
limiter microbench. Production numbers: single-shot read-only probes
(`fide_cli.sh shell` timings, `EXPLAIN`, `fide-verify`, log inspection)
— never load-tested, never written to for benchmarking.
