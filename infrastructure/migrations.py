"""Own migration history for the FIDE Service. No Swiss reuse.

Versioned ``upgrade`` functions in ``migrations/versions/`` run in order
and are recorded in the ``schema_migrations`` table, so upgrades are
idempotent and auditable. Downgrades are intentionally NOT supported in
Phase 1 (restore from backup instead) — a deliberate scope decision.
"""
import importlib
import logging
from typing import List, Tuple

from sqlalchemy import text

log = logging.getLogger(__name__)

#: (version, module) in apply order. Append-only: never reorder, never edit
#: an applied migration — add a new one.
MIGRATIONS: List[Tuple[str, str]] = [
    ("0001_initial", "migrations.versions.v0001_initial"),
    ("0002_widen_text_columns", "migrations.versions.v0002_widen_text_columns"),
    ("0003_drop_redundant_rating_index",
     "migrations.versions.v0003_drop_redundant_rating_index"),
]


def _ensure_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version VARCHAR(32) PRIMARY KEY, "
            " applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        ))


def applied_versions(engine) -> List[str]:
    _ensure_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT version FROM schema_migrations ORDER BY version")).all()
    return [r[0] for r in rows]


def pending_migrations(engine) -> List[str]:
    applied = set(applied_versions(engine))
    return [v for v, _ in MIGRATIONS if v not in applied]


def upgrade(engine) -> List[str]:
    """Apply every pending migration in order. Returns applied versions."""
    applied_now: List[str] = []
    for version, module_name in MIGRATIONS:
        if version in set(applied_versions(engine)):
            continue
        module = importlib.import_module(module_name)
        log.info("Applying migration %s: %s", version,
                 getattr(module, "DESCRIPTION", ""))
        module.upgrade(engine)
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:v)"),
                {"v": version})
        applied_now.append(version)
        log.info("Applied migration %s", version)
    return applied_now
