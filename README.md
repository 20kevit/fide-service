# FIDE Service — Architecture / Documentation Workspace

> Status (Phase 0): **documentation only — no implementation yet.**
> This repository will become the independent FIDE Service. It is currently
> an architecture and extraction-planning workspace for the future Flask
> application. Do not treat these docs as the implementation.

## Repository relationship

```text
G:\Programming\Web\
├── swiss_app_dev\      ← Swiss Manager (independent Git repository)
└── fide_service\       ← FIDE Service (THIS repository, independent Git history)
```

* `swiss_app_dev` and `fide_service` are **two separate Git repositories**
  with completely separate histories.
* `fide_service` is **NOT** a subdirectory, submodule, or subtree of
  `swiss_app_dev`, and is not managed by it.
* Phase 0 made **zero** changes to `swiss_app_dev`. All work in this repo
  is new documentation scaffolding.

## Target direction

```text
Swiss Manager
      │
      │ HTTPS REST API (service-to-service API key, fail closed)
      ▼
FIDE Service (own Flask app, own DB, own venv, own config,
              own migrations, own storage, own workers/Cron,
              own deployment, own tests)
```

Core invariant: **FIDE = current FIDE data; Swiss = tournament snapshot.**
Tournament operations must keep working when the FIDE Service is
unavailable, using local snapshots and graceful degradation
(see `docs/03-data-ownership.md` and `docs/08-failure-and-resilience.md`).

## What is in this repo right now

```text
fide_service/
├── README.md                        ← this file
└── docs/
    ├── 01-current-state.md          ← existing FIDE inside Swiss Manager
    ├── 02-dependency-map.md         ← every Swiss → FIDE consumer
    ├── 03-data-ownership.md         ← FIDE-owned vs Swiss-owned fields
    ├── 04-database.md               ← FIDE tables, constraints, migrations
    ├── 05-api-contract.md           ← REST API v1 draft
    ├── 06-swiss-client-contract.md  ← interface Swiss must depend on
    ├── 07-import-architecture.md    ← legacy vs job-based import pipeline
    ├── 08-failure-and-resilience.md ← failure matrix
    ├── 09-deployment.md             ← independent deployment design
    ├── 10-testing-strategy.md       ← test plan + coupling inventory
    ├── 11-extraction-plan.md        ← migration strategy
    ├── 12-open-decisions.md         ← DECIDED / RECOMMENDED / UNKNOWN / BLOCKER
    └── FIDE_EXTRACTION_ARCHITECTURE.md ← executive reference for coding agents
```

## Phase discipline

* **Phase 0 (this phase):** architecture + documentation only. No feature
  implementation, no Swiss code changes, no migrations run, no production
  changes, no legacy deletion.
* **Known Swiss-side issue (documented, NOT fixed here):**
  `templates/tournament/player_edit.html` exposes neither `fide_id` nor
  `k_factor`, while `ParticipantManagement.update` reads both from the form
  (missing key → wipe/reset). See `docs/01-current-state.md` §12 and
  `docs/12-open-decisions.md`. It must be addressed during extraction
  planning, not silently forgotten.
* **Do not begin Phase 1 implementation** after reading these docs unless
  explicitly instructed. The extraction plan (`docs/11-extraction-plan.md`)
  is the entry point when implementation is authorized.

## Evidence convention used in these docs

Every important conclusion cites concrete repository evidence as
`path[:line]` relative to `swiss_app_dev` (e.g.
`infrastructure/models/fide.py:13`, `config.py:59`). Three labels are used:

```text
CURRENT BEHAVIOR     — what the code does today (verified by reading it)
INTENDED BEHAVIOR    — what the approved domain decisions require
RECOMMENDED FUTURE   — what the FIDE Service should do (explicit proposal)
```

## Source of truth

* Executive reference: `docs/FIDE_EXTRACTION_ARCHITECTURE.md`
* API draft: `docs/05-api-contract.md`
* Swiss-side interface: `docs/06-swiss-client-contract.md`
* Roadmap: `docs/11-extraction-plan.md`
* Unknowns: `docs/12-open-decisions.md`
