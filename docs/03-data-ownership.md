# 03 — Data Ownership: FIDE vs Swiss

## 1. Approved identity decision (INTENDED BEHAVIOR — must not change silently)

Inside FIDE Service:

* Every player MUST have a FIDE ID.
* `fide_id` is NOT NULL.
* `fide_id` is UNIQUE.
* `fide_id` is the primary identity / business key.

Inside Swiss Manager:

* `fide_id` MAY be NULL.
* Swiss may contain players with no FIDE ID.
* When present, `fide_id` identifies the corresponding FIDE player.

CURRENT evidence: `infrastructure/models/fide.py:13`
(`FidePlayerModel.fide_id` PK → NOT NULL + unique by PK);
`infrastructure/models/profile.py:14`
(`PlayerProfileModel.fide_id String(20) nullable index`, **not** unique —
uniqueness enforced in app: `verification_request_service` blocks duplicate
pending/verified claims, `status_updater` blocks duplicate verified);
`domain/fide/models.py` (`FidePlayerData.fide_id`), `validation.py`
(`REQUIRED_PLAYER_FIELDS` includes `fideid`, parser skips blank `fideid`).

## 2. Ownership table

| Field | Owner | Authoritative source | CURRENT BEHAVIOR | Notes |
|---|---|---|---|---|
| `fide_id` (FIDE side) | FIDE Service | Monthly FIDE XML (`players_list_xml` / per-type `{standard,rapid,blitz}_{mon}{yy}frl_xml.zip`) | PK, merge key on every import | Business key; never synthesized |
| `fide_id` (Swiss side) | Swiss Manager | Local `player_profiles.fide_id` (nullable, user/arbiter-supplied or CSV) | May be `NULL`/`""`; wiped by buggy edit path (see §5) | Link, not copy of authority |
| `name, sex, federation, birth_year, inactive` | FIDE Service | FIDE XML → `fide_players` | Overwritten on every import in both pipelines | Swiss `player_profiles` names are independent local data |
| `title, wtitle, otitle, foatitle` | FIDE Service (official list) | FIDE XML → `fide_players` | Overwritten on every import | See `01-current-state.md` §11 for sync rule |
| `profile.fide_title` | Swiss Manager | Verification approval / arbiter edit / CSV | Set from `title \|\| wtitle \|\| otitle \|\| foatitle` on approval; copied to snapshot on create/update; `P0-F` blocks player input | Owned local copy, not live |
| `participant.fide_title_snapshot` | Swiss Manager | Frozen at `create` (or CSV/import) from `profile.fide_title` | Consumed by standings/print/export; never re-fetched | Tournament truth |
| `rating, games, k_factor` per `(period, rating_type)` | FIDE Service | FIDE XML → `fide_ratings` (UQ `(fide_id, period, rating_type)`) | Idempotent upsert, `BATCH_SIZE=500` | Current ratings + history |
| `participant.rating_snapshot, k_factor` | Swiss Manager | Frozen at `create` (auto-fetch when `rating==0 and profile.fide_id`, else form/CSV) | `update` path has no auto-fetch; `k` defaults to 20 | Tournament truth; pairing/prints use this |
| `fide_verification_status`, verification queue (`requested_fide_id`, per-aspect `fide_id/dob/photo` verdicts) | Swiss Manager | Local verification workflow (`verification_request_service`, `verification_approver`, `status_updater`) | Requires local `FidePlayer` row to exist (fail closed) | After extraction: verification reads FIDE via API but the workflow state stays Swiss-owned |
| Import processing state (`fide_imports` row, `fide_import_jobs` rows, staging files, heartbeats, progress) | FIDE Service | Import worker | `ensure → tick → claim → download → validate → stream-import → delete ZIP`; stale → `WORKER_STALE`, explicit retry | Moves entirely; Swiss must never own this |
| Pricing / eligibility inputs derived from FIDE (`PlayerPricingData.fide_title`, `RequirementSet.fide_verification_required`) | Swiss Manager | Local `profile.fide_title` / `fide_verification_status` | Never live-lookup at pricing time | Snapshot-driven by design |

## 3. Authoritative-source / snapshot / synchronized classification

* **Authoritative (FIDE):** identity fields, four title columns, per-period
  per-type `(rating, games, k_factor)`, dataset period/version.
* **Snapshot (Swiss, frozen):** `rating_snapshot`, `k_factor`,
  `fide_title_snapshot` on `tournament_participants`; reads in standings,
  prints, exports use snapshots only (`print_routes.py:170-186`,
  `custom_json_provider.py:212,327,410`, `import_service.py:76,96,257`).
* **Synchronized (Swiss copy of FIDE at an event):** `profile.fide_title`
  (on verification approval), `profile.fide_id` (on claim/create/edit/CSV),
  `participant` snapshots (on create). No background live-sync exists today.
* **Ambiguous ownership (CURRENT, must be resolved — see `12-open-decisions.md`):**
  1. `PlayerProfileModel.fide_id` non-unique at DB level vs app-level
     uniqueness for pending/verified — decide whether Swiss adds a partial
     unique constraint or keeps app enforcement.
  2. `birth_year String(4)` storing `<birthday>` text — decide canonical
     representation in FIDE Service (keep verbatim vs normalize).
  3. Empty official title on re-approval (keep existing vs clear) — CURRENT
     keeps existing; confirm as INTENDED.
  4. Reactivation path in `registration_approver.py` (sets
     `fide_title_snapshot` but never re-fetches rating) — confirm frozen
     vs resync semantic.

## 4. Core invariant

```text
FIDE = Current FIDE Data
Swiss = Tournament Snapshot
```

Tournament operations must continue correctly when the FIDE Service is
unavailable (snapshots, graceful degradation, explicit fail-closed only
where the business rule requires actual verification — see
`docs/08-failure-and-resilience.md`).

## 5. `fide_id` and the participant-edit bug (CURRENT BEHAVIOR)

`templates/tournament/player_edit.html` exposes neither `fide_id` nor
`k_factor`, yet `ParticipantManagement.update` reads both
(`profile.fide_id = form["fide_id"]` → wipe; `k_factor → 20` reset).
This is a **Swiss-side ownership violation**: the edit form destroys link
state it never displays. DO NOT FIX in Phase 0. Preserve the invariant in
the client contract: `fide_id`/`k_factor` are immutable without an explicit
`resync` operation, and missing keys must never mean wipe
(see `docs/06-swiss-client-contract.md`).
