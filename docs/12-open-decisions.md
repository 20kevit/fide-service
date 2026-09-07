# 12 — Open Decisions

Classify every item: `DECIDED` (approved, do not revisit silently),
`RECOMMENDED` (proposal, confirm in Phase 1), `UNKNOWN` (genuine gap, do
not guess), `BLOCKER` (must resolve before the indicated phase).

## DECIDED

1. `fide_id` NOT NULL + UNIQUE + business key inside FIDE Service; NULLABLE
   in Swiss (may be absent). — Domain decision §1.
2. Data ownership: FIDE = current data/ratings/history/import lifecycle;
   Swiss = local players/participants/registration/snapshots/tournament
   truth. Tournament flows continue when FIDE is down. — §2.
3. Boundary is REST over HTTPS; Swiss never touches the FIDE DB/ORM/repos/
   filesystem after extraction. — §§4–5.
4. FIDE Service is NOT IRI-only by design; IRI-only registration/history is
   a current Swiss product restriction (`FIDE_ALLOWED_FEDERATIONS=["IRI"]`,
   `config.py:59`). — §3.
5. Service-to-service API key, fail closed, no secrets in logs, env-supplied
   secrets, no unnecessary auth infra. — §5.
6. Four title columns (`title/wtitle/otitle/foatitle`) preserved; current
   sync priority `title → wtitle → otitle → foatitle`, first non-empty wins
   (`verification_approver.py:95-102`, `verification_status_updater.py:50-57`).
   No new rule invented in Phase 0. — §6.
7. Failure philosophy: degrade gracefully except fail-closed verification.
   — §7.
8. Separate Git repos/histories; no submodule; small revertible commits;
   Phase 0 = docs only, no implementation. — repo rules.

## RECOMMENDED

9. Header `X-API-Key` + separate admin scope/key; error envelope
   `{error:{code,message,details,request_id}}`; `datasetagan` block on every
   data response. — needs Phase 1 confirmation (`05-api-contract.md`).
10. Job-based pipeline as the sole pipeline; orchestrator retired; uploads
    as job types; `fide_imports` becomes rollup/history. (`07`)
11. Client timeouts connect ≤ 3 s / read ≤ 8 s, one GET retry; player/ratings
    cache 1 h, search 60 s LRU, history long TTL; snapshots win offline. (`08`)
12. Swiss client layout `application/fide_client/{client,dto,errors,cache,
    fallback}.py` with no-FIDE-ORM rule + no-silent-wipe invariant. (`06`)
13. FIDE-only migration chain seeded from `bb0160… + e7a2… + b3f9…`; Python
    3.12 prod parity; Cron worker renamed `fide-worker`. (`09`)

## UNKNOWN

14. Exact search length floor + `total` semantics (CURRENT: service default
    `limit=20`, dashboard `limit=10` + `<3 chars → []`, admin
    `federation=IRI` default vs dashboard `None`). Confirm in Phase 1. (`05`)
15. Stale-period threshold N for verification approval + whether `status`
    auth reuses the read key. (`08`, `05`)
16. `birth_year String(4)` canonical form (verbatim `<birthday>` text vs
    normalized year) and empty-official-title-on-reapproval (CURRENT keeps
    existing — confirm as INTENDED). (`03`, `04`)
17. Production paths/quota/host/venv/DB names, backup retention, log
    destinations, rate-limit values. Marked `[CONFIRM]` in `09`. Never invent.
18. Reaction to upstream `http://` base (CURRENT `config.py:60,73`) from an
    HTTPS-only service: keep upstream HTTP vs proxy/translate — confirm with
    ops in Phase 1.
19. Actual row counts + per-period growth + query plans (code cites ~1.9 M
    rows/file; unmeasured in target DB). Measure in Phase 2. (`04`)

## BLOCKER

20. **Migration `b3f9c2d74a61_fide_import_jobs.py` unapplied** (header:
    "HAS NOT BEEN APPLIED anywhere"). Explicit approval + rehearsal required
    before Phase 1/2 touches a real DB. — blocks Phase 1 DB work.
21. **Swiss edit-form data loss** (`player_edit.html` no `fide_id`/`k_factor`,
    truncated titles; `ParticipantManagement.update` wipes/resets). Fix
    design required before Phase 4 cutover; regression test fails until
    fixed. Documented, NOT fixed in Phase 0. — blocks Phase 4.
22. **API-key issuance/storage/rotation + admin scope** — must exist before
    Phase 2 serves traffic or Phase 3 shadow calls authenticate. — blocks
    Phase 2/3.
23. **Swiss `fide_*` table freeze/drop window + dump/restore rehearsal** —
    required before Phase 5 data movement. — blocks Phase 5.
