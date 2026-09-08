# 16 — Data Integrity & Correctness (Phase 6)

Labels: `TEST-VERIFIED` (suite assertion), `PRODUCTION-VERIFIED`
(observed on live data), `INFERRED` (code-read, no direct test),
`UNVERIFIED` (not measurable from here).

## 1. Invariants

| # | Invariant | Status | Enforcement |
|---|---|---|---|
| 1 | Every rating belongs to an existing player | TEST-VERIFIED + PRODUCTION-VERIFIED | FK enforced on MySQL and (since Phase 6) SQLite via connect-time pragma; `fide-verify` orphan check as second net; production orphans = 0 |
| 2 | No duplicate `(fide_id, period, rating_type)` | TEST-VERIFIED + PRODUCTION-VERIFIED | `uq_fide_rating_period_type` on both engines; upsert-last-wins; production duplicates = 0 |
| 3 | One FIDE ID = one player | TEST-VERIFIED | PK; leading-zero IDs preserved as distinct strings |
| 4 | No Standard/Rapid/Blitz cross-contamination | TEST-VERIFIED | Per-type job isolation + parser `rating_type` mapping; tick-level test with distinctive values |
| 5 | Period associations correct | TEST-VERIFIED (trust boundary documented) | Job period trusted (see §3); download URL encodes period; uploads report a filename verdict |
| 6 | Re-import convergent and idempotent | TEST-VERIFIED | Full double-run converges to identical logical snapshots |
| 7 | Duplicate source rows cannot duplicate DB rows | TEST-VERIFIED | UQ + update path; intra-file, cross-batch, and boundary duplicates tested |
| 8 | Invalid source data cannot corrupt valid data | TEST-VERIFIED | Lenient row policies (§2); malformed archives fail the job with zero rows |
| 9 | Failed imports never falsely complete | TEST-VERIFIED | `verifying` gate (`processed>0` and `stored>0`); cleanup errors warn instead of failing |
| 10 | Verification fails closed | TEST-VERIFIED | `ok` requires zero duplicates/nulls/invalid-types/orphans (vacuous-`ok` on an empty DB is documented, not relied upon — the import gate fails closed separately) |
| 11 | Pruning cannot delete protected/current data | TEST-VERIFIED | `period < keep_period` only, per-type, keep-list federations exempt; current period never pruned; empty keep-list prunes nothing |
| 12 | API returns database state verbatim | TEST-VERIFIED + PRODUCTION-VERIFIED | Field-by-field API-vs-DB test locally; sampled players checked live |
| 13 | Pagination neither omits nor duplicates | TEST-VERIFIED | Ascending `(period, type)` order; limit/offset slicing + bounds tested |
| 14 | Batch semantics deterministic | TEST-VERIFIED | Input-order dedup, `missing` for unknown-but-well-formed IDs, 400 for empty/oversize/unusable bodies |
| 15 | Crash recovery preserves integrity | TEST-VERIFIED | 33 recovery tests (claim/commit/prune/verify/cleanup boundaries) |
| 16 | Concurrent workers cannot corrupt state | TEST-VERIFIED (SQLite) + Phase-2 MySQL locking evidence | Claim protocol; second worker backs off |
| 17 | Constraints back the critical invariants | TEST-VERIFIED | PK ×1, UQ ×2, FK ×1 enforced on both engines; no CHECK constraints (invalid types/states are *detected* by verify/claim logic, not rejected by DDL — deliberate, see §4) |
| 18 | Restore is verifiable | TEST-VERIFIED (SQLite cycle) | Backup → restore → counts + `verify ok` + snapshot equality; production MySQL dumps follow the same verify step (RUNBOOK) |

## 2. Parse & normalization policy (verbatim-first, lenient)

* IDs and names are strings: no integer conversion, no trimming, no case
  folding. Leading-zero IDs are distinct. Whitespace-only names and
  empty IDs are skipped at import (counted as neither processed nor
  stored); padded-but-nonblank names are stored verbatim.
* Unparseable rating/games/K become `0`; missing tags become `""`
  (strings) or `0` (ints); missing `<sex>` becomes `"M"`, an empty
  `<sex/>` becomes `""`. Missing country becomes `""`. These reflect
  official files always carrying the fields — TEST-VERIFIED as
  deterministic behavior, not as validation rules. Do not tighten
  without a specification: dropping rows risks losing real players.
* Identity is current-wins: name, federation, titles, birth year, and
  inactive flag are overwritten by every period that carries the player.
  History lives only in `fide_ratings` rows, retained per §5.
* Overlong IDs (>20 chars, e.g. hostile input) store fine on SQLite but
  abort the batch on MySQL strict mode — fail-closed, no corruption.
  Official IDs never trigger this. INFERRED for MySQL (from strict-mode
  semantics + the production 1406 precedent); no code change: truncating
  would corrupt, skipping would drop data.

## 3. Period trust boundary

The XML carries no period metadata, so `job.period` is trusted
unconditionally once a file is staged or downloaded:

* Download path: safe by construction — `build_rating_url` derives the
  exact official archive name from (type, period).
* Upload path: `POST /admin/uploads` now returns `period_match`
  (official filename vs job period, case-insensitive) and logs a
  warning on mismatch. Mismatches still stage (never a rejection —
  emergency restores must not be blocked); the operator must confirm
  contents. Server-side manual staging has no verdict: use official
  names and confirm via `fide-verify` counts.
* A mislabeled file imports under the job period with the file's values
  (TEST-VERIFIED as behavior, not as approval). Gross errors surface in
  `fide-verify` counts; subtle month swaps rely on operator discipline.

## 4. Deliberate non-constraints

* No `CHECK` on `rating_type`, `period`, or job `state`: invalid values
  are *detectable* (`invalid_rating_types` fails verification; unknown
  job states are inert to claiming and recoverable to `failed`), and
  adding DDL would require a production migration with restore-based
  rollback for no observed failure. Revisit only on evidence.
* `total_records` on jobs is the validator's sample cap (2000), not a
  volume metric — do not alert on it.

## 5. Retention rules

* Identity + current-period ratings exist for ALL federations.
* Older periods survive only for `HISTORY_FEDERATIONS` (default `IRI`);
  empty value keeps everything. Pruning is per (period, rating_type),
  strictly `period < keep_period`, committed only after verification.
* `latest`/`history`/period APIs resolve against retained rows; a
  pruned-away past returns `null`/empty, never another period's data.

## 6. Backup / restore verification

Procedure (RUNBOOK): nightly MySQL dump + restore rehearsal before
cutover-class operations. Verification after ANY restore: `fide-verify`
(`ok` true, sane counts), spot-check known players, confirm
`latest_period`. A disposable SQLite backup→restore→verify cycle is
covered by the suite as a procedure proof; MySQL dump/restore remains
an operator rehearsal item (UNVERIFIED from here).
